#!/usr/bin/env python3
"""Stage C — cited wiki paper pages.

One page per paper: BULK_MODEL writes the corpus.yaml-required headings' prose
with [n] citations resolving to that paper's own section-tree nodes. Enforces
two contracts via generate-and-retry: all required headings present, and every
[n] marker has a matching, real citation. Deterministic parts (frontmatter,
related-papers scoring, ## Links footer) are templated, never LLM-authored.

Output: kb/wiki/papers/<doc_id>.md + kb/wiki/papers/<doc_id>.citations.json

Usage:
    python3 kb/paper_pages.py --dry-run
    python3 kb/paper_pages.py
    python3 kb/paper_pages.py --only 2013Icar..225..432S --force
    python3 kb/paper_pages.py --frontmatter-only
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml
from openai import OpenAI
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))
from kb import config

sys.path.insert(0, str(Path(__file__).parent.parent / "webapp"))
from app.answer import find_uncited_author_year_mentions

PROMPT_VERSION = "1"
RETRIES = 3   # 4 total attempts — citation-heavy papers (100+ citations) need more
              # headroom than kb/classify.py's coarse tags did; see max_tokens below
MAX_REFERENCE_NODES_SHOWN = 40   # cap bibliography entries in the citation pool —
                                  # rarely what Results/Method sections cite,
                                  # and some papers have 60+ of them
TABLE_TEXT_PREVIEW_CHARS = 400    # raw <table> HTML can be huge; truncate in the pool
MAX_RELATED = 5

client = OpenAI(base_url=config.BASE_URL, api_key=config.API_KEY)


# ---------------------------------------------------------------------------
# Node pool — the closed set of (node_id, text) an LLM call may cite
# ---------------------------------------------------------------------------

def flatten_leaves(nodes: list[dict]) -> list[dict]:
    out = []
    for n in nodes:
        if n["kind"] == "container":
            out.extend(flatten_leaves(n["nodes"]))
        else:
            out.append(n)
    return out


def build_node_pool(section_tree: dict) -> tuple[str, set[str]]:
    """Returns (pool_text_for_prompt, set_of_valid_node_ids)."""
    leaves = flatten_leaves(section_tree["structure"])
    references = [n for n in leaves if n["block_type"] == "reference"]
    non_references = [n for n in leaves if n["block_type"] != "reference"]
    shown = non_references + references[:MAX_REFERENCE_NODES_SHOWN]

    lines = []
    valid_ids = set()
    for n in shown:
        text = n["text"]
        if n["block_type"] == "table":
            text = text[:TABLE_TEXT_PREVIEW_CHARS]
        lines.append(f"[{n['node_id']}] ({n['block_type']}) {text}")
        valid_ids.add(n["node_id"])
    return "\n".join(lines), valid_ids


# ---------------------------------------------------------------------------
# LLM call — one page, all required headings, in one shot
# ---------------------------------------------------------------------------

def build_prompt(headings: list[str], title: str, metadata_yaml: str, pool_text: str) -> tuple[str, str]:
    heading_list = "\n".join(f"- {h}" for h in headings)
    system = (
        "You write one wiki page for a paper in a knowledge base about the "
        "exoplanet WASP-12b. Write exactly these H2 sections, in this order, "
        "each starting with '## <heading>':\n" + heading_list + "\n\n"
        "Every claim must carry an inline [n] citation marker, where n refers to "
        "an entry in the citations list you return. Cite ONLY node_ids from the "
        "numbered pool below — never invent one, never cite a node not shown.\n\n"
        "CITATION RULE: if you name a specific source by author/year in your "
        "prose, that exact sentence must ALSO carry its [n] — never write an "
        "author-year mention as a substitute for [n], always attach both.\n"
        "  BAD:  'Madhusudhan et al. (2011) reported a carbon-rich atmosphere, "
        "in contrast to the host star.'\n"
        "  GOOD: 'Madhusudhan et al. (2011) reported a carbon-rich atmosphere "
        "[2], in contrast to the host star [2].'\n\n"
        "If a section genuinely doesn't apply to this paper (e.g. "
        "'WASP-12b-Specific Findings' for a paper that only mentions it in a "
        "table), write one honest sentence saying so, citing the node that shows "
        "the mention if one exists.\n\n"
        "Respond with a single fenced json block:\n"
        "```json\n"
        '{"body_markdown": "## Overview\\n...\\n\\n## Observations & Data\\n...",\n'
        ' "citations": [{"id": 1, "node_id": "0008"}, {"id": 2, "node_id": "0042"}]}\n'
        "```\n"
        "citations ids are numbered sequentially across the whole page, in the "
        "order they first appear in body_markdown, matching the [n] markers used."
    )
    user = (
        f"# Metadata\n{metadata_yaml}\n"
        f"# Title\n{title}\n\n"
        f"# Citable node pool (cite ONLY these node_ids)\n{pool_text}"
    )
    return system, user


def parse_response(text: str) -> dict | None:
    m = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    block = m.group(1) if m else text
    try:
        return json.loads(block)
    except json.JSONDecodeError:
        return None


CITATION_MARKER_RE = re.compile(r"\[(\d+)\]")


def validate(result: dict, headings: list[str], valid_node_ids: set[str]) -> str | None:
    if not isinstance(result, dict):
        return "response is not a mapping"
    body = result.get("body_markdown")
    citations = result.get("citations")
    if not isinstance(body, str) or not body.strip():
        return "missing or empty body_markdown"
    if not isinstance(citations, list):
        return "missing or non-list citations"

    for h in headings:
        if f"## {h}" not in body:
            return f"missing required heading '## {h}'"

    citation_ids = set()
    for c in citations:
        if not isinstance(c, dict) or "id" not in c or "node_id" not in c:
            return f"malformed citation entry: {c!r}"
        citation_ids.add(c["id"])
        if c["node_id"] not in valid_node_ids:
            return f"citation cites unknown node_id: {c['node_id']!r}"

    used_markers = {int(m) for m in CITATION_MARKER_RE.findall(body)}
    unresolved = used_markers - citation_ids
    if unresolved:
        return f"[n] marker(s) with no matching citation entry: {sorted(unresolved)}"
    return None


def headings_missing_citations(body: str, headings: list[str]) -> list[str]:
    """Non-fatal report: which headings have zero [n] markers in their prose."""
    missing = []
    for i, h in enumerate(headings):
        start = body.find(f"## {h}")
        if start == -1:
            continue
        end = body.find("## ", start + 1)
        segment = body[start:end if end != -1 else len(body)]
        if not CITATION_MARKER_RE.search(segment):
            missing.append(h)
    return missing


def thin_signals(body: str, headings: list[str]) -> list[str]:
    """Non-fatal report combining zero-citation headings with the
    author-year-mention heuristic (webapp/app/answer.py) — the same "names a
    study but the [n] is missing/too far away" check the chat flow uses,
    applied here as a report only (NOT a hard validation failure): unlike
    interactive chat, a batch job re-running this on every heuristic false
    positive risks real added cost with no guaranteed improvement, since the
    heuristic has a known false-positive rate on long, correctly-cited
    sentences (verified against live chat output)."""
    signals = [f"heading '{h}' has no citations" for h in headings_missing_citations(body, headings)]
    signals += [f"possible uncited mention: {m}" for m in find_uncited_author_year_mentions(body)]
    return signals


def generate_page(doc_id: str, title: str, metadata_yaml: str, headings: list[str],
                   pool_text: str, valid_node_ids: set[str]) -> tuple[dict, list[str]]:
    system, user = build_prompt(headings, title, metadata_yaml, pool_text)
    last_error = None
    for attempt in range(RETRIES + 1):
        max_tokens = 8000 * (2 ** attempt)  # 8000, 16000, 32000, 64000 — same
                                              # escalation rationale as
                                              # kb/classify.py, but starting
                                              # higher: verified failures here
                                              # include a citations[] array
                                              # truncated mid-list on heavily-
                                              # cited papers (100+ citations),
                                              # not just reasoning-token
                                              # exhaustion
        resp = client.chat.completions.create(
            model=config.BULK_MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.2,
            max_tokens=max_tokens,
        )
        text = resp.choices[0].message.content or ""
        result = parse_response(text)
        if result is not None:
            error = validate(result, headings, valid_node_ids)
            if error is None:
                thin = thin_signals(result["body_markdown"], headings)
                return result, thin
            last_error = error
        else:
            last_error = "could not parse json block from response"
        if attempt < RETRIES:
            time.sleep(2 ** attempt)
    raise ValueError(f"page generation failed after {RETRIES + 1} attempts: {last_error}")


# ---------------------------------------------------------------------------
# Deterministic parts — related papers, links footer, frontmatter
# ---------------------------------------------------------------------------

def related_papers(doc_id: str, classification: dict, all_classifications: dict[str, dict],
                    limit: int = MAX_RELATED) -> list[dict]:
    my_theme = set(classification.get("research_theme") or [])
    my_facility = set(classification.get("facility") or [])
    my_role = classification.get("role_of_wasp12b")
    scored = []
    for other_id, other in all_classifications.items():
        if other_id == doc_id:
            continue
        score = (2 * len(my_theme & set(other.get("research_theme") or []))
                 + 2 * len(my_facility & set(other.get("facility") or []))
                 + (1 if other.get("role_of_wasp12b") == my_role else 0))
        if score > 0:
            scored.append((score, other_id))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [{"doc_id": d, "score": s} for s, d in scored[:limit]]


def slug_for(kind: str, value: str) -> str:
    return f"{kind}-{config.slugify(value)}"


def links_footer(classification: dict, related: list[dict], paper: dict,
                  all_papers_by_id: dict, doi: str | None) -> str:
    lines = ["## Links"]
    themes = classification.get("research_theme") or []
    if themes:
        theme_links = " | ".join(f"[{t}](../topics/{slug_for('theme', t)}.md)" for t in themes)
        lines.append(f"- Themes: {theme_links}")
    facilities = classification.get("facility") or []
    if facilities:
        fac_links = " | ".join(f"[{f}](../topics/{slug_for('facility', f)}.md)" for f in facilities)
        lines.append(f"- Facility: {fac_links}")
    if related:
        rel_links = ", ".join(
            f"[{r['doc_id']}]({r['doc_id']}.md) \"{all_papers_by_id.get(r['doc_id'], {}).get('title', '')}\""
            for r in related)
        lines.append(f"- Related papers: {rel_links}")
    ext = []
    bibcode = paper.get("bibcode")
    if bibcode:
        ext.append(f"[ADS](https://ui.adsabs.harvard.edu/abs/{bibcode})")
    if doi:
        ext.append(f"[DOI](https://doi.org/{doi})")
    if ext:
        lines.append(f"- External: {' | '.join(ext)}")
    return "\n".join(lines)


def front_matter(paper: dict, classification: dict, related: list[dict],
                  fulltext_source: str) -> str:
    fm = {
        "doc_id": paper["doc_id"],
        "title": paper["title"],
        "year": paper.get("year"),
        "journal": paper.get("journal"),
        "authors": paper.get("authors"),
        "fulltext_source": fulltext_source,
        "research_theme": classification.get("research_theme") or [],
        "method": classification.get("method") or [],
        "facility": classification.get("facility") or [],
        "role_of_wasp12b": classification.get("role_of_wasp12b"),
        "related_papers": related,
        "generator": {
            "model": config.BULK_MODEL, "prompt_version": PROMPT_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    }
    return yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def page_prompt_version(doc_id: str) -> str | None:
    path = config.WIKI_PAPERS_DIR / f"{doc_id}.md"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    m = re.search(r"generator:\s*\n(?:\s+\S.*\n)*?\s*prompt_version:\s*['\"]?(\S+)['\"]?", text)
    return m.group(1).strip("'\"") if m else None


def write_page(doc_id: str, paper: dict, classification: dict, page: dict,
                related: list[dict], fulltext_source: str, all_papers_by_id: dict) -> None:
    fm = front_matter(paper, classification, related, fulltext_source)
    footer = links_footer(classification, related, paper, all_papers_by_id,
                           paper.get("extra", {}).get("doi"))
    content = f"---\n{fm}---\n\n# {paper['title']}\n\n{page['body_markdown']}\n\n{footer}\n"
    out_path = config.WIKI_PAPERS_DIR / f"{doc_id}.md"
    tmp = out_path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, out_path)

    citations_path = config.WIKI_PAPERS_DIR / f"{doc_id}.citations.json"
    tmp2 = citations_path.with_suffix(".citations.json.tmp")
    tmp2.write_text(json.dumps({"doc_id": doc_id, "citations": page["citations"]},
                                ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp2, citations_path)


def process_paper(doc_id: str, paper: dict, classification: dict,
                   all_classifications: dict, headings: list[str],
                   papers_by_id: dict) -> tuple[str, str | None, list[str]]:
    section_tree = json.loads((config.SECTIONS_DIR / f"{doc_id}.json").read_text(encoding="utf-8"))
    pool_text, valid_node_ids = build_node_pool(section_tree)
    metadata_yaml = yaml.safe_dump({
        "title": paper["title"], "year": paper.get("year"), "journal": paper.get("journal"),
        "research_theme": classification.get("research_theme"),
        "method": classification.get("method"), "facility": classification.get("facility"),
        "role_of_wasp12b": classification.get("role_of_wasp12b"),
    }, sort_keys=False)

    try:
        page, thin_headings = generate_page(doc_id, paper["title"], metadata_yaml,
                                             headings, pool_text, valid_node_ids)
    except Exception as e:
        return doc_id, str(e), []

    related = related_papers(doc_id, classification, all_classifications)
    fulltext_source = paper.get("fulltext_source", "publisher_pdf")
    write_page(doc_id, paper, classification, page, related, fulltext_source, papers_by_id)
    return doc_id, None, thin_headings


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate cited wiki paper pages.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--only", metavar="DOC_ID")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--frontmatter-only", action="store_true")
    parser.add_argument("--regenerate-if-prompt-changed", action="store_true")
    args = parser.parse_args()

    if not config.API_KEY:
        sys.exit("API_KEY not found in .env")

    cfg = config.load_corpus_config()
    headings = cfg["wiki_headings"]

    papers_by_id = {p["doc_id"]: p for p in config.load_papers()}
    for doc_id, p in papers_by_id.items():
        p["fulltext_source"] = "publisher_pdf"
    # honor the one known arXiv-preprint substitution (see docs/spec/00_overview.md)
    if "2010RPPh...73a6901B" in papers_by_id:
        papers_by_id["2010RPPh...73a6901B"]["fulltext_source"] = "arxiv_preprint"

    all_classifications = {}
    for f in config.CLASSIFICATION_DIR.glob("*.yaml"):
        all_classifications[f.stem] = yaml.safe_load(f.read_text(encoding="utf-8"))

    section_files = sorted(config.SECTIONS_DIR.glob("*.json"))
    todo = []
    done = 0
    for f in section_files:
        doc_id = f.stem
        if args.only and doc_id != args.only:
            continue
        if doc_id not in all_classifications:
            continue  # Stage B hasn't classified this one yet
        if not args.force:
            existing_version = page_prompt_version(doc_id)
            if existing_version == PROMPT_VERSION:
                done += 1
                continue
            if existing_version is not None and not args.regenerate_if_prompt_changed:
                done += 1
                continue
        todo.append(doc_id)
    if args.limit:
        todo = todo[:args.limit]

    print(f"Section trees: {len(section_files)} | already generated: {done} | "
          f"to generate: {len(todo)}")
    if args.dry_run:
        for d in todo:
            print(f"  [dry-run] {d}")
        return
    if not todo:
        return

    failed = []
    thin_report = {}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(process_paper, doc_id, papers_by_id[doc_id],
                             all_classifications[doc_id], all_classifications,
                             headings, papers_by_id): doc_id
            for doc_id in todo
        }
        with tqdm(total=len(futures), unit="paper", desc="Generating pages") as pbar:
            for future in as_completed(futures):
                doc_id, error, thin = future.result()
                if error:
                    tqdm.write(f"  FAILED {doc_id}: {error}")
                    failed.append(doc_id)
                elif thin:
                    thin_report[doc_id] = thin
                pbar.update(1)

    print(f"\nGenerated: {len(todo) - len(failed)} | failed: {len(failed)}")
    for d in failed:
        print(f"  FAILED {d}")
    if thin_report:
        print(f"\nCitation signals (non-fatal — zero-citation headings are expected for a "
              f"genuinely inapplicable section; uncited-mention hits have a real "
              f"false-positive rate on long, correctly-cited sentences — spot-check both):")
        for d, signals_list in thin_report.items():
            print(f"  {d}: {', '.join(signals_list)}")


if __name__ == "__main__":
    main()
