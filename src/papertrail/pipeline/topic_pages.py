#!/usr/bin/env python3
"""Stage D — cross-paper cited topic pages (one per taxonomy facet value) + index.

Map/reduce: each member paper's own digest-heading sections (already
[n]-cited prose from Stage C — which headings count as the digest is
corpus.yaml's navigation.digest_headings) are the "map" digest; SYNTH_MODEL
reads those digests plus the actual node text each digest cites (resolved
from citations.json + section-tree JSON, spanning multiple papers) and
writes a synthesis that cites primary nodes directly — never another wiki
page's citation, so "click a citation, see the original text" holds for
topic pages exactly as it does for chat/paper pages. Programmatic year pages
stay uncited and byte-stable (no timestamp) across reruns.

Which facets get topic pages, their slugs, and which facet groups the doc
tree are all corpus.yaml-driven (papertrail migration Step 9, Seam 3) — this
stage no longer hardcodes "theme/method/facility".

Output: <corpus>/wiki/topics/<slug_prefix>-<slug>.md + .citations.json, <corpus>/wiki/index.md

Usage:
    papertrail topic-pages --dry-run
    papertrail topic-pages
    papertrail topic-pages --force
    papertrail topic-pages --lint
"""
import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import yaml
from tqdm import tqdm

from .. import prompts
from ..config import Settings, load_settings
from ..core.citations import find_uncited_author_year_mentions
from ..core.nodes import iter_leaves
from ..identity import load_citation_style
from ..taxonomy import FacetSpec, topic_page_facets
from ..textutils import slugify
from .llm import get_client

RETRIES = 3   # 4 total attempts — same lesson as kb/paper_pages.py: large
              # topic groups (dozens of member papers, wide citation pools)
              # need more headroom than a small default budget provides


# ---------------------------------------------------------------------------
# Load everything Stage C/B produced
# ---------------------------------------------------------------------------

def load_all_classifications(settings: Settings) -> dict[str, dict]:
    out = {}
    for f in settings.classification_dir.glob("*.yaml"):
        out[f.stem] = yaml.safe_load(f.read_text(encoding="utf-8"))
    return out


def load_wiki_page(settings: Settings, doc_id: str) -> tuple[str, dict] | None:
    """Returns (body_markdown_without_frontmatter, citations_dict) or None if
    no paper page exists yet for this doc_id (Stage C not run / no fulltext)."""
    md_path = settings.wiki_dir / "papers" / f"{doc_id}.md"
    cit_path = settings.wiki_dir / "papers" / f"{doc_id}.citations.json"
    if not md_path.exists() or not cit_path.exists():
        return None
    text = md_path.read_text(encoding="utf-8")
    parts = text.split("---\n", 2)
    body = parts[2] if len(parts) == 3 else text
    citations = json.loads(cit_path.read_text(encoding="utf-8"))
    by_id = {c["id"]: c["node_id"] for c in citations["citations"]}
    return body, by_id


MARKER_RE = re.compile(r"\[(\d+)\]")


def extract_section(body: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", body,
                  re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


def paper_digest(settings: Settings, doc_id: str, title: str, year: int | None,
                  digest_headings: list[str]) -> tuple[str, set[str]]:
    """Returns (digest_text_with_local_markers_intact, set_of_node_ids_it_cites).
    Digest text keeps its original [n] markers as-is — the caller resolves them
    against this doc's own citation map when building the shared pool.

    `digest_headings` is corpus.yaml's navigation.digest_headings (was a
    hardcoded DIGEST_SECTIONS module constant) — renaming a wiki heading
    without updating this used to fail every topic with a misleading "no
    member paper has a wiki page yet"; load_navigation() now validates
    digest_headings against wiki_headings at config-load time instead."""
    loaded = load_wiki_page(settings, doc_id)
    if loaded is None:
        return "", set()
    body, cit_by_id = loaded
    pieces = []
    cited_node_ids = set()
    for heading in digest_headings:
        section_text = extract_section(body, heading)
        if not section_text:
            continue
        pieces.append(f"### {heading}\n{section_text}")
        for marker in MARKER_RE.findall(section_text):
            node_id = cit_by_id.get(int(marker))
            if node_id:
                cited_node_ids.add(node_id)
    header = f"[{doc_id}] {title} ({year})"
    return f"{header}\n" + "\n".join(pieces), cited_node_ids


def node_text_for(settings: Settings, doc_id: str, node_id: str, _cache: dict = {}) -> str | None:
    if doc_id not in _cache:
        path = settings.sections_dir / f"{doc_id}.json"
        _cache[doc_id] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    tree = _cache[doc_id]
    if tree is None:
        return None
    for n in iter_leaves(tree["structure"]):
        if n["node_id"] == node_id:
            return n["text"]
    return None


# ---------------------------------------------------------------------------
# Synthesis LLM call
# ---------------------------------------------------------------------------

def build_topic_prompt(settings: Settings, kind: str, name: str, digests: list[str],
                        pool_lines: list[str]) -> tuple[str, str]:
    """The system prompt's citation example is corpus.yaml's one canonical
    citation_example (papertrail migration Step 9, Seam 2) — this used to
    have its OWN separate hardcoded example (Yee et al. 2020) distinct from
    paper_pages.py's (Madhusudhan et al. 2011); consolidated to one example
    per corpus rather than asking a corpus author to write several."""
    system = prompts.render(settings.corpus, "topic_page.system")
    user = (
        f"# Topic: {kind} = \"{name}\" — {len(digests)} papers\n\n"
        f"# Paper digests\n\n" + "\n\n".join(digests) + "\n\n"
        f"# Citable node pool (cite ONLY these (doc_id, node_id) pairs)\n" +
        "\n".join(pool_lines)
    )
    return system, user


def parse_response(text: str) -> dict | None:
    """Parse the ``` json block SYNTH_MODEL returns, tolerating two malformation
    classes observed in practice on this model/route (verified against 23 real
    failures captured from a live run, not hypothesized):

    1. Literal newlines inside the "prose" string, unescaped -- the model
       writes genuine multi-paragraph prose as one JSON string value with real
       line breaks rather than \\n. Strict JSON forbids raw control characters
       in a string; `strict=False` is the documented, lossless way to allow
       them -- this is relaxing an unnecessarily strict default, not a repair.
       Fixed ~40% of the captured failures outright with no ambiguity.

    2. A stray brace/bracket or an unescaped internal quote somewhere in the
       long free-text prose, at an unpredictable position each time (no fixed
       pattern -- checked across 17 residual failures, no two broke at the
       same construct). For this class only, fall back to `json_repair`,
       which is built for exactly this LLM-output failure mode. Its guess is
       NOT trusted blindly: the caller's existing `validate_topic()` still
       enforces dict-shape, non-empty prose, and non-empty citations, so a
       repair that guesses wrong (observed once: reinterpreted the whole
       object as a list) is rejected and retried like any other failure,
       never silently accepted.
    """
    m = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    block = m.group(1) if m else text
    try:
        return json.loads(block, strict=False)
    except json.JSONDecodeError:
        pass
    try:
        import json_repair
        result = json_repair.loads(block)
    except Exception:
        return None
    return result if isinstance(result, dict) else None


def validate_topic(result: dict, valid_pairs: set[tuple[str, str]]) -> str | None:
    if not isinstance(result, dict):
        return "response is not a mapping"
    prose = result.get("prose")
    citations = result.get("citations")
    if not isinstance(prose, str) or not prose.strip():
        return "missing or empty prose"
    if not isinstance(citations, list) or not citations:
        return "missing or empty citations"

    citation_ids = set()
    for c in citations:
        if not isinstance(c, dict) or not {"id", "doc_id", "node_id"} <= c.keys():
            return f"malformed citation entry: {c!r}"
        citation_ids.add(c["id"])
        if (c["doc_id"], c["node_id"]) not in valid_pairs:
            return f"citation cites unknown (doc_id,node_id): {(c['doc_id'], c['node_id'])!r}"

    used_markers = {int(m) for m in MARKER_RE.findall(prose)}
    unresolved = used_markers - citation_ids
    if unresolved:
        return f"[n] marker(s) with no matching citation entry: {sorted(unresolved)}"
    return None


def synthesize_topic(settings: Settings, client, kind: str, name: str, member_docs: list[dict],
                      digest_headings: list[str]) -> dict:
    digests = []
    pool_lines = []
    valid_pairs = set()
    for doc_id, title, year in [(d["doc_id"], d["title"], d.get("year")) for d in member_docs]:
        digest, cited_node_ids = paper_digest(settings, doc_id, title, year, digest_headings)
        if not digest:
            continue
        digests.append(digest)
        for node_id in sorted(cited_node_ids):
            text = node_text_for(settings, doc_id, node_id)
            if text is None:
                continue
            pool_lines.append(f"[{doc_id} / {node_id}] {text[:300]}")
            valid_pairs.add((doc_id, node_id))

    if not digests:
        raise ValueError(
            f"no member paper has a wiki page containing any of the digest headings "
            f"{list(digest_headings)!r} — Stage C not run for this group, or "
            f"navigation.digest_headings doesn't match the generated pages")

    system, user = build_topic_prompt(settings, kind, name, digests, pool_lines)
    last_error = None
    for attempt in range(RETRIES + 1):
        max_tokens = 8000 * (2 ** attempt)  # 8000, 16000, 32000, 64000
        resp = client.chat.completions.create(
            model=settings.synth_model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_tokens=max_tokens,
            # SYNTH_MODEL (an Opus-tier model on this gateway) rejects a
            # temperature param on some routes — omit it, matching kasi's
            # documented workaround for the same issue.
        )
        text = resp.choices[0].message.content or ""
        result = parse_response(text)
        if result is not None:
            error = validate_topic(result, valid_pairs)
            if error is None:
                return result
            last_error = error
        else:
            last_error = "could not parse json block from response"
        if attempt < RETRIES:
            time.sleep(2 ** attempt)
    raise ValueError(f"topic synthesis failed after {RETRIES + 1} attempts: {last_error}")


# ---------------------------------------------------------------------------
# Deterministic parts — member table, year/index pages
# ---------------------------------------------------------------------------

def member_table(member_docs: list[dict], taxonomy: dict[str, FacetSpec], table_columns: list[str],
                  papers_prefix: str = "../papers/") -> str:
    headers = ["Year", "Paper"] + [taxonomy[c].label for c in table_columns]
    rows = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for d in sorted(member_docs, key=lambda x: (x.get("year") or 0, x["doc_id"])):
        title = (d["title"] or "").replace("|", "-")
        cells = [str(d.get("year") or ""), f"[{title}]({papers_prefix}{d['doc_id']}.md)"]
        for col in table_columns:
            v = d.get(col)
            cells.append(", ".join(v) if isinstance(v, list) else (v or ""))
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows)


def write_topic_page(settings: Settings, slug: str, kind: str, name: str, title: str,
                      prose_or_none: str, member_docs: list[dict], model: str,
                      taxonomy: dict[str, FacetSpec], table_columns: list[str],
                      citations: list[dict] | None = None) -> None:
    fm = {"title": title, "kind": kind, "paper_count": len(member_docs),
          "generator": {"model": model}}
    if model != "programmatic":
        fm["generator"]["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fm_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
    body = prose_or_none or f"{len(member_docs)} papers in this corpus fall under {kind} \"{name}\"."
    content = (f"---\n{fm_yaml}---\n\n# {title}\n\n{body}\n\n"
               f"## Papers\n\n{member_table(member_docs, taxonomy, table_columns)}\n\n"
               f"[← KB index](../index.md)\n")
    out_path = settings.wiki_dir / "topics" / f"{slug}.md"
    tmp = out_path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, out_path)

    if citations is not None:
        cit_path = settings.wiki_dir / "topics" / f"{slug}.citations.json"
        tmp2 = cit_path.with_suffix(".citations.json.tmp")
        tmp2.write_text(json.dumps({"slug": slug, "citations": citations},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp2, cit_path)


def group_papers(all_docs: list[dict], facet: str) -> dict[str, list[dict]]:
    groups = defaultdict(list)
    for d in all_docs:
        values = d.get(facet)
        if isinstance(values, list):
            for v in values:
                groups[v].append(d)
        elif values:
            groups[values].append(d)
    return groups


def build_index(settings: Settings, client, all_docs: list[dict], groups_by_facet: dict[str, dict],
                 year_groups: dict, taxonomy: dict[str, FacetSpec], table_columns: list[str],
                 force: bool) -> None:
    out_path = settings.wiki_dir / "index.md"
    if out_path.exists() and not force:
        return

    stats = {"total_papers": len(all_docs),
            "by_year": {y: len(d) for y, d in sorted(year_groups.items())}}
    for facet, groups in groups_by_facet.items():
        stats[f"by_{facet}"] = {k: len(v) for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))}

    # a modest pool for the index's high-level prose: the first topic-page
    # facet's top 10 groups, one representative (most recent) paper each.
    digests, pool_lines, valid_pairs = [], [], set()
    first_facet = next(iter(groups_by_facet), None)
    digest_headings = settings.corpus.navigation.digest_headings
    if first_facet:
        for name, docs in list(groups_by_facet[first_facet].items())[:10]:
            rep = sorted(docs, key=lambda d: d.get("year") or 0)[-1]
            digest, cited_node_ids = paper_digest(settings, rep["doc_id"], rep["title"], rep.get("year"),
                                                  digest_headings)
            if digest:
                digests.append(digest)
                for node_id in sorted(cited_node_ids):
                    text = node_text_for(settings, rep["doc_id"], node_id)
                    if text is not None:
                        pool_lines.append(f"[{rep['doc_id']} / {node_id}] {text[:300]}")
                        valid_pairs.add((rep["doc_id"], node_id))

    prose = None
    citations = None
    if digests:
        system = prompts.render(settings.corpus, "index_intro.system")
        user = (f"# Corpus stats\n{yaml.safe_dump(stats, sort_keys=False)}\n\n"
                f"# Representative paper digests\n\n" + "\n\n".join(digests) + "\n\n"
                f"# Citable node pool\n" + "\n".join(pool_lines))
        last_error = None
        for attempt in range(RETRIES + 1):
            resp = client.chat.completions.create(
                model=settings.synth_model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=3000 * (2 ** attempt),
            )
            result = parse_response(resp.choices[0].message.content or "")
            if result and not validate_topic(result, valid_pairs):
                prose, citations = result["prose"], result["citations"]
                break
            last_error = "index prose generation failed validation"
            time.sleep(2 ** attempt)
        else:
            tqdm.write(f"  index.md prose FAILED: {last_error} — writing stats-only index")

    lines = [f"# {settings.corpus.title}\n"]
    if prose:
        lines.append(prose + "\n")
    for i, (facet, groups) in enumerate(groups_by_facet.items()):
        label = taxonomy[facet].label
        lines.append(("\n" if i > 0 else "") + f"## By {label.lower()}")
        for k, n in stats[f"by_{facet}"].items():
            lines.append(f"- [{k}](topics/{slugify(taxonomy[facet].slug_prefix + '-' + k)}.md) ({n} papers)")
    lines.append("\n## By year")
    for y in sorted(stats["by_year"]):
        lines.append(f"- [{y}](topics/year-{y}.md) ({stats['by_year'][y]} papers)")
    lines.append("\n## All papers\n")
    lines.append(member_table(all_docs, taxonomy, table_columns, papers_prefix="papers/"))

    tmp = out_path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, out_path)
    if citations is not None:
        cit_path = settings.wiki_dir / "index.citations.json"
        cit_path.write_text(json.dumps({"citations": citations}, ensure_ascii=False, indent=2),
                             encoding="utf-8")


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------

LINK_RE = re.compile(r"\[[^\]]*\]\((?!https?://|mailto:)([^)#]+)\)")


def lint(settings: Settings) -> bool:
    ok = True
    for md_file in settings.wiki_dir.rglob("*.md"):
        text = md_file.read_text(encoding="utf-8")
        for link in LINK_RE.findall(text):
            target = (md_file.parent / link).resolve()
            if not target.exists():
                print(f"BROKEN LINK in {md_file}: {link}")
                ok = False
    return ok


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate cross-paper topic/index pages.")
    parser.add_argument("--corpus", metavar="PATH")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--lint", action="store_true")
    return parser


def run(settings: Settings, args) -> None:
    if args.lint:
        sys.exit(0 if lint(settings) else 1)

    if not settings.api_key:
        sys.exit("API_KEY not found in .env")

    taxonomy = settings.corpus.taxonomy
    nav = settings.corpus.navigation
    facets = topic_page_facets(taxonomy)

    papers_by_id = {p["doc_id"]: p for p in settings.corpus.load_papers()}
    classifications = load_all_classifications(settings)

    all_docs = []
    for doc_id, cls in classifications.items():
        p = papers_by_id.get(doc_id, {})
        doc = {"doc_id": doc_id, "title": p.get("title", ""), "year": p.get("year")}
        for facet in taxonomy:
            doc[facet] = cls.get(facet) or ([] if taxonomy[facet].multi else None)
        all_docs.append(doc)

    groups_by_facet = {spec.name: group_papers(all_docs, spec.name) for spec in facets}
    year_groups = defaultdict(list)
    if nav.year_pages:
        for d in all_docs:
            if d["year"]:
                year_groups[d["year"]].append(d)

    todo = []
    for spec in facets:
        for name, members in groups_by_facet[spec.name].items():
            slug = f"{spec.slug_prefix}-{slugify(name)}"
            out_path = settings.wiki_dir / "topics" / f"{slug}.md"
            if out_path.exists() and not args.force:
                continue
            todo.append((spec, name, members))

    print(f"Facet groups: {sum(len(g) for g in groups_by_facet.values())} | "
          f"to generate: {len(todo)} | year pages: {len(year_groups)} (always regenerated)")
    if args.dry_run:
        for spec, name, members in todo:
            print(f"  [dry-run] {spec.slug_prefix}-{slugify(name)} ({len(members)} papers)")
        print("  [dry-run] index.md")
        return

    client = get_client(settings)
    style = load_citation_style(settings.corpus)
    failed = []
    thin_report = {}
    for spec, name, members in tqdm(todo, unit="topic", desc="Synthesizing topics"):
        slug = f"{spec.slug_prefix}-{slugify(name)}"
        try:
            result = synthesize_topic(settings, client, spec.slug_prefix, name, members, nav.digest_headings)
        except Exception as e:
            tqdm.write(f"  FAILED {slug}: {e}")
            failed.append(slug)
            continue
        write_topic_page(settings, slug, spec.slug_prefix, name, f"{spec.label}: {name}",
                          result["prose"], members, settings.synth_model, taxonomy, nav.table_columns,
                          result["citations"])
        mentions = find_uncited_author_year_mentions(result["prose"], style)
        if mentions:  # non-fatal report only — see kb/paper_pages.py's thin_signals docstring
            thin_report[slug] = mentions

    # Year pages — always regenerated, programmatic, uncited, byte-stable.
    for year, members in year_groups.items():
        write_topic_page(settings, f"year-{year}", "year", str(year), f"Year: {year}",
                          None, members, "programmatic", taxonomy, nav.table_columns)

    build_index(settings, client, all_docs, groups_by_facet, year_groups, taxonomy,
                nav.table_columns, args.force)

    print(f"\nGenerated: {len(todo) - len(failed)} | failed: {len(failed)}")
    for f in failed:
        print(f"  FAILED {f}")
    if thin_report:
        print(f"\nPossible uncited/loosely-cited mentions (non-fatal, worth a spot-check):")
        for slug, mentions in thin_report.items():
            print(f"  {slug}: {mentions}")

    print("\nRunning lint...")
    sys.exit(0 if lint(settings) else 1)


def main(argv=None) -> None:
    args = build_arg_parser().parse_args(argv)
    settings = load_settings(args.corpus, create_dirs=True)
    run(settings, args)


if __name__ == "__main__":
    main()
