#!/usr/bin/env python3
"""Stage D — cross-paper cited topic/theme/method/facility pages + index.

Map/reduce: each member paper's own Overview + WASP-12b-Specific Findings
sections (already [n]-cited prose from Stage C) are the "map" digest; SYNTH_MODEL
reads those digests plus the actual node text each digest cites (resolved from
citations.json + section-tree JSON, spanning multiple papers) and writes a
synthesis that cites primary nodes directly — never another wiki page's
citation, so "click a citation, see the original text" holds for topic pages
exactly as it does for chat/paper pages. Programmatic year pages stay uncited
and byte-stable (no timestamp) across reruns.

Output: kb/wiki/topics/<kind>-<slug>.md + .citations.json, kb/wiki/index.md

Usage:
    python3 kb/topic_pages.py --dry-run
    python3 kb/topic_pages.py
    python3 kb/topic_pages.py --force
    python3 kb/topic_pages.py --lint
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml
from openai import OpenAI
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))
from kb import config, paper_pages

RETRIES = 3   # 4 total attempts — same lesson as kb/paper_pages.py: large
              # topic groups (dozens of member papers, wide citation pools)
              # need more headroom than a small default budget provides

client = OpenAI(base_url=config.BASE_URL, api_key=config.API_KEY)


# ---------------------------------------------------------------------------
# Load everything Stage C/B produced
# ---------------------------------------------------------------------------

def load_all_classifications() -> dict[str, dict]:
    out = {}
    for f in config.CLASSIFICATION_DIR.glob("*.yaml"):
        out[f.stem] = yaml.safe_load(f.read_text(encoding="utf-8"))
    return out


def load_wiki_page(doc_id: str) -> tuple[str, dict] | None:
    """Returns (body_markdown_without_frontmatter, citations_dict) or None if
    no paper page exists yet for this doc_id (Stage C not run / no fulltext)."""
    md_path = config.WIKI_PAPERS_DIR / f"{doc_id}.md"
    cit_path = config.WIKI_PAPERS_DIR / f"{doc_id}.citations.json"
    if not md_path.exists() or not cit_path.exists():
        return None
    text = md_path.read_text(encoding="utf-8")
    parts = text.split("---\n", 2)
    body = parts[2] if len(parts) == 3 else text
    citations = json.loads(cit_path.read_text(encoding="utf-8"))
    by_id = {c["id"]: c["node_id"] for c in citations["citations"]}
    return body, by_id


DIGEST_SECTIONS = ("Overview", "WASP-12b-Specific Findings")
MARKER_RE = re.compile(r"\[(\d+)\]")


def extract_section(body: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", body,
                  re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


def paper_digest(doc_id: str, title: str, year: int | None) -> tuple[str, set[str]]:
    """Returns (digest_text_with_local_markers_intact, set_of_node_ids_it_cites).
    Digest text keeps its original [n] markers as-is — the caller resolves them
    against this doc's own citation map when building the shared pool."""
    loaded = load_wiki_page(doc_id)
    if loaded is None:
        return "", set()
    body, cit_by_id = loaded
    pieces = []
    cited_node_ids = set()
    for heading in DIGEST_SECTIONS:
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


def node_text_for(doc_id: str, node_id: str, _cache: dict = {}) -> str | None:
    if doc_id not in _cache:
        path = config.SECTIONS_DIR / f"{doc_id}.json"
        _cache[doc_id] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    tree = _cache[doc_id]
    if tree is None:
        return None
    for n in paper_pages.flatten_leaves(tree["structure"]):
        if n["node_id"] == node_id:
            return n["text"]
    return None


# ---------------------------------------------------------------------------
# Synthesis LLM call
# ---------------------------------------------------------------------------

def build_topic_prompt(kind: str, name: str, digests: list[str], pool_lines: list[str]) -> tuple[str, str]:
    system = (
        "You write one topic page synthesizing across multiple papers in a "
        "knowledge base about the exoplanet WASP-12b. Write 2-4 analytical "
        "paragraphs of prose — no headings, no lists, no links — covering how "
        "the papers approach this topic, how understanding evolved over time, "
        "which papers stand out and why, and any visible gaps or disagreements. "
        "Ground every claim in the provided paper digests and node pool; do not "
        "invent papers, results, or numbers not present below.\n\n"
        "Every claim must carry an inline [n] citation marker, where n refers to "
        "an entry in the citations list you return. Cite ONLY (doc_id, node_id) "
        "pairs from the numbered pool below — never invent one.\n\n"
        "CITATION RULE: if you name a specific paper by author/year in your "
        "prose, that exact sentence must ALSO carry its [n] — never write an "
        "author-year mention as a substitute for [n], always attach both.\n"
        "  BAD:  'Yee et al. (2020) confirmed the orbital decay, resolving "
        "the earlier ambiguity.'\n"
        "  GOOD: 'Yee et al. (2020) confirmed the orbital decay [3], "
        "resolving the earlier ambiguity [3].'\n\n"
        "Respond with a single fenced json block:\n"
        "```json\n"
        '{"prose": "paragraph one [1]...\\n\\nparagraph two [2]...",\n'
        ' "citations": [{"id": 1, "doc_id": "...", "node_id": "0008"}, '
        '{"id": 2, "doc_id": "...", "node_id": "0042"}]}\n'
        "```"
    )
    user = (
        f"# Topic: {kind} = \"{name}\" — {len(digests)} papers\n\n"
        f"# Paper digests\n\n" + "\n\n".join(digests) + "\n\n"
        f"# Citable node pool (cite ONLY these (doc_id, node_id) pairs)\n" +
        "\n".join(pool_lines)
    )
    return system, user


def parse_response(text: str) -> dict | None:
    m = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    block = m.group(1) if m else text
    try:
        return json.loads(block)
    except json.JSONDecodeError:
        return None


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


def synthesize_topic(kind: str, name: str, member_docs: list[dict]) -> dict:
    digests = []
    pool_lines = []
    valid_pairs = set()
    for doc_id, title, year in [(d["doc_id"], d["title"], d.get("year")) for d in member_docs]:
        digest, cited_node_ids = paper_digest(doc_id, title, year)
        if not digest:
            continue
        digests.append(digest)
        for node_id in sorted(cited_node_ids):
            text = node_text_for(doc_id, node_id)
            if text is None:
                continue
            pool_lines.append(f"[{doc_id} / {node_id}] {text[:300]}")
            valid_pairs.add((doc_id, node_id))

    if not digests:
        raise ValueError("no member paper has a wiki page yet (Stage C not run for this group)")

    system, user = build_topic_prompt(kind, name, digests, pool_lines)
    last_error = None
    for attempt in range(RETRIES + 1):
        max_tokens = 8000 * (2 ** attempt)  # 8000, 16000, 32000, 64000
        resp = client.chat.completions.create(
            model=config.SYNTH_MODEL,
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

def member_table(member_docs: list[dict], papers_prefix: str = "../papers/") -> str:
    rows = ["| Year | Paper | Role | Facility |", "|---|---|---|---|"]
    for d in sorted(member_docs, key=lambda x: (x.get("year") or 0, x["doc_id"])):
        title = (d["title"] or "").replace("|", "-")
        facility = ", ".join(d.get("facility") or [])
        rows.append(f"| {d.get('year', '')} | [{title}]({papers_prefix}{d['doc_id']}.md) | "
                    f"{d.get('role_of_wasp12b', '')} | {facility} |")
    return "\n".join(rows)


def write_topic_page(kind: str, name: str, prose_or_none: str, member_docs: list[dict],
                      model: str, citations: list[dict] | None = None) -> None:
    slug = f"{kind}-{config.slugify(name)}"
    fm = {"title": f"{kind.title()}: {name}", "kind": kind, "paper_count": len(member_docs),
          "generator": {"model": model}}
    if model != "programmatic":
        fm["generator"]["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fm_yaml = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
    body = prose_or_none or f"{len(member_docs)} papers in this corpus fall under {kind} \"{name}\"."
    content = (f"---\n{fm_yaml}---\n\n# {kind.title()}: {name}\n\n{body}\n\n"
               f"## Papers\n\n{member_table(member_docs)}\n\n[← KB index](../index.md)\n")
    out_path = config.WIKI_TOPICS_DIR / f"{slug}.md"
    tmp = out_path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, out_path)

    if citations is not None:
        cit_path = config.WIKI_TOPICS_DIR / f"{slug}.citations.json"
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


def build_index(all_docs: list[dict], theme_groups: dict, method_groups: dict,
                 facility_groups: dict, year_groups: dict, force: bool) -> None:
    out_path = config.WIKI_DIR / "index.md"
    if out_path.exists() and not force:
        return

    stats = {
        "total_papers": len(all_docs),
        "by_year": {y: len(d) for y, d in sorted(year_groups.items())},
        "by_theme": {k: len(v) for k, v in sorted(theme_groups.items(), key=lambda kv: -len(kv[1]))},
        "by_method": {k: len(v) for k, v in sorted(method_groups.items(), key=lambda kv: -len(kv[1]))},
        "by_facility": {k: len(v) for k, v in sorted(facility_groups.items(), key=lambda kv: -len(kv[1]))},
    }

    # a modest pool for the index's high-level prose: each theme's top paper by year
    digests, pool_lines, valid_pairs = [], [], set()
    for theme, docs in list(theme_groups.items())[:10]:
        rep = sorted(docs, key=lambda d: d.get("year") or 0)[-1]
        digest, cited_node_ids = paper_digest(rep["doc_id"], rep["title"], rep.get("year"))
        if digest:
            digests.append(digest)
            for node_id in sorted(cited_node_ids):
                text = node_text_for(rep["doc_id"], node_id)
                if text is not None:
                    pool_lines.append(f"[{rep['doc_id']} / {node_id}] {text[:300]}")
                    valid_pairs.add((rep["doc_id"], node_id))

    prose = None
    citations = None
    if digests:
        system = (
            "You write the welcoming introduction to a knowledge base about the "
            "exoplanet WASP-12b, covering all papers that mention it. Write 2-3 "
            "welcoming, analytical paragraphs of markdown prose — no headings, "
            "no lists, no links — summarizing the corpus's scope and evolution. "
            "Every claim must carry an inline [n] citation marker resolving to "
            "the citations list you return, citing ONLY (doc_id, node_id) pairs "
            "from the pool below.\n\n"
            "Respond with a single fenced json block:\n"
            '```json\n{"prose": "...", "citations": [{"id": 1, "doc_id": "...", "node_id": "..."}]}\n```'
        )
        user = (f"# Corpus stats\n{yaml.safe_dump(stats, sort_keys=False)}\n\n"
                f"# Representative paper digests\n\n" + "\n\n".join(digests) + "\n\n"
                f"# Citable node pool\n" + "\n".join(pool_lines))
        last_error = None
        for attempt in range(RETRIES + 1):
            resp = client.chat.completions.create(
                model=config.SYNTH_MODEL,
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

    lines = ["# WASP-12b Knowledge Base\n"]
    if prose:
        lines.append(prose + "\n")
    lines.append("## By theme")
    for k, n in stats["by_theme"].items():
        lines.append(f"- [{k}](topics/{config.slugify('theme-' + k)}.md) ({n} papers)")
    lines.append("\n## By method")
    for k, n in stats["by_method"].items():
        lines.append(f"- [{k}](topics/{config.slugify('method-' + k)}.md) ({n} papers)")
    lines.append("\n## By facility")
    for k, n in stats["by_facility"].items():
        lines.append(f"- [{k}](topics/{config.slugify('facility-' + k)}.md) ({n} papers)")
    lines.append("\n## By year")
    for y in sorted(stats["by_year"]):
        lines.append(f"- [{y}](topics/year-{y}.md) ({stats['by_year'][y]} papers)")
    lines.append("\n## All papers\n")
    lines.append(member_table(all_docs, papers_prefix="papers/"))

    tmp = out_path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, out_path)
    if citations is not None:
        cit_path = config.WIKI_DIR / "index.citations.json"
        cit_path.write_text(json.dumps({"citations": citations}, ensure_ascii=False, indent=2),
                             encoding="utf-8")


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------

LINK_RE = re.compile(r"\[[^\]]*\]\((?!https?://|mailto:)([^)#]+)\)")


def lint() -> bool:
    ok = True
    for md_file in config.WIKI_DIR.rglob("*.md"):
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

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate cross-paper topic/index pages.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--lint", action="store_true")
    args = parser.parse_args()

    if args.lint:
        sys.exit(0 if lint() else 1)

    if not config.API_KEY:
        sys.exit("API_KEY not found in .env")

    papers_by_id = {p["doc_id"]: p for p in config.load_papers()}
    classifications = load_all_classifications()

    all_docs = []
    for doc_id, cls in classifications.items():
        p = papers_by_id.get(doc_id, {})
        all_docs.append({
            "doc_id": doc_id, "title": p.get("title", ""), "year": p.get("year"),
            "research_theme": cls.get("research_theme") or [],
            "method": cls.get("method") or [],
            "facility": cls.get("facility") or [],
            "role_of_wasp12b": cls.get("role_of_wasp12b"),
        })

    theme_groups = group_papers(all_docs, "research_theme")
    method_groups = group_papers(all_docs, "method")
    facility_groups = group_papers(all_docs, "facility")
    year_groups = defaultdict(list)
    for d in all_docs:
        if d["year"]:
            year_groups[d["year"]].append(d)

    todo = []
    for kind, groups in (("theme", theme_groups), ("method", method_groups), ("facility", facility_groups)):
        for name, members in groups.items():
            slug = f"{kind}-{config.slugify(name)}"
            out_path = config.WIKI_TOPICS_DIR / f"{slug}.md"
            if out_path.exists() and not args.force:
                continue
            todo.append((kind, name, members))

    print(f"Facet groups: {sum(len(g) for g in (theme_groups, method_groups, facility_groups))} | "
          f"to generate: {len(todo)} | year pages: {len(year_groups)} (always regenerated)")
    if args.dry_run:
        for kind, name, members in todo:
            print(f"  [dry-run] {kind}-{config.slugify(name)} ({len(members)} papers)")
        print("  [dry-run] index.md")
        return

    failed = []
    thin_report = {}
    for kind, name, members in tqdm(todo, unit="topic", desc="Synthesizing topics"):
        try:
            result = synthesize_topic(kind, name, members)
        except Exception as e:
            tqdm.write(f"  FAILED {kind}-{config.slugify(name)}: {e}")
            failed.append(f"{kind}-{name}")
            continue
        write_topic_page(kind, name, result["prose"], members, config.SYNTH_MODEL, result["citations"])
        mentions = paper_pages.find_uncited_author_year_mentions(result["prose"])
        if mentions:  # non-fatal report only — see kb/paper_pages.py's thin_signals docstring
            thin_report[f"{kind}-{config.slugify(name)}"] = mentions

    # Year pages — always regenerated, programmatic, uncited, byte-stable.
    for year, members in year_groups.items():
        write_topic_page("year", str(year), None, members, "programmatic")

    build_index(all_docs, theme_groups, method_groups, facility_groups, year_groups, args.force)

    print(f"\nGenerated: {len(todo) - len(failed)} | failed: {len(failed)}")
    for f in failed:
        print(f"  FAILED {f}")
    if thin_report:
        print(f"\nPossible uncited/loosely-cited mentions (non-fatal, worth a spot-check):")
        for slug, mentions in thin_report.items():
            print(f"  {slug}: {mentions}")

    print("\nRunning lint...")
    sys.exit(0 if lint() else 1)


if __name__ == "__main__":
    main()
