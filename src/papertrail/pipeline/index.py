#!/usr/bin/env python3
"""Stage E — build kb.sqlite.

Full drop-and-rebuild of the content tables (docs, nodes, doc_facets,
chunks_fts) every run — no resume logic needed at this scale
(kasi_papers_analysis's precedent). answers/conversations/messages are
created once (core.schema.ensure_persistent) and NEVER dropped by a
rebuild, so re-running the pipeline after adding papers doesn't destroy
chat history.

Nodes are indexed directly from <corpus>/data/sections/*.json — one row per leaf
AND container node — with no resplitting step (unlike kasi's kb, nodes here are
already paragraph/table/figure-sized, so each is already a natural FTS chunk).
Wiki page bodies (paper/topic pages) are NOT separately indexed: their prose
already exists as the primary-source node(s) it cites elsewhere in `nodes`, and
indexing it twice would let two rows answer the same search hit with different
provenance. Wiki pages remain browsable independent of the FTS index.

The 20 stub docs (no fulltext — see docs/spec/00_overview.md) get one node row
each of block_type='stub' so they're findable by title/author search.

Facet storage (papertrail migration Step 7): each doc's facet values go into
`docs.facets_json` (one dict, `{facet: value_or_list}`) AND `doc_facets`
(one row per (doc_id, facet, value) pair, including single-valued facets —
see core/schema.py's docstring). The four facet names themselves
(research_theme/method/facility/role_of_wasp12b) are still wasp12b-specific
literals here — making that taxonomy-driven is Step 9's job (Seam 3
remainder), not this one.

Usage:
    papertrail index
"""
import argparse
import json
import sqlite3

import yaml
from tqdm import tqdm

from ..config import Settings, load_settings
from ..core import schema

FACET_NAMES = ("research_theme", "method", "facility", "role_of_wasp12b")


def load_classification(settings: Settings, doc_id: str) -> dict:
    path = settings.classification_dir / f"{doc_id}.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _facets_for(cls: dict) -> dict:
    return {facet: cls.get(facet) for facet in FACET_NAMES if cls.get(facet) is not None}


def _insert_doc_facets(conn: sqlite3.Connection, doc_id: str, facets: dict) -> None:
    for facet, value in facets.items():
        values = value if isinstance(value, list) else [value]
        for v in values:
            if v is None or v == []:
                continue
            conn.execute("INSERT INTO doc_facets (doc_id, facet, value) VALUES (?,?,?)",
                        (doc_id, facet, v))


def insert_full_doc(conn: sqlite3.Connection, settings: Settings, doc_id: str, paper: dict,
                     cls: dict, fulltext_source: str) -> None:
    facets = _facets_for(cls)
    conn.execute(
        "INSERT INTO docs (doc_id, title, year, journal, authors_json, "
        "fulltext_source, has_fulltext, facets_json) VALUES (?,?,?,?,?,?,?,?)",
        (doc_id, paper.get("title"), paper.get("year"), paper.get("journal"),
         json.dumps(paper.get("authors")), fulltext_source, 1,
         json.dumps(facets, ensure_ascii=False)))
    _insert_doc_facets(conn, doc_id, facets)

    section_tree = json.loads((settings.sections_dir / f"{doc_id}.json").read_text(encoding="utf-8"))

    def walk(nodes: list[dict], parent_title: str | None):
        for n in nodes:
            if n["kind"] == "container":
                conn.execute(
                    "INSERT INTO nodes (doc_id, node_id, kind, block_type, "
                    "parent_title, page, text) VALUES (?,?,?,?,?,?,?)",
                    (doc_id, n["node_id"], "container", None, parent_title, None, n["title"]))
                walk(n["nodes"], n["title"])
            else:
                conn.execute(
                    "INSERT INTO nodes (doc_id, node_id, kind, block_type, "
                    "parent_title, page, text) VALUES (?,?,?,?,?,?,?)",
                    (doc_id, n["node_id"], "leaf", n["block_type"], parent_title,
                     n.get("page"), n["text"]))

    walk(section_tree["structure"], None)


def insert_stub_doc(conn: sqlite3.Connection, entry: dict) -> None:
    doc_id = entry["doc_id"]
    conn.execute(
        "INSERT INTO docs (doc_id, title, year, journal, authors_json, "
        "fulltext_source, has_fulltext, facets_json) VALUES (?,?,?,?,?,?,?,?)",
        (doc_id, entry.get("title"), entry.get("year"), entry.get("journal"),
         json.dumps(entry.get("authors")), None, 0, "{}"))
    authors = entry.get("authors") or ""
    stub_text = f"{entry.get('title', '')}. {authors}. {entry.get('journal', '')} ({entry.get('year', '')})."
    conn.execute(
        "INSERT INTO nodes (doc_id, node_id, kind, block_type, parent_title, "
        "page, text) VALUES (?,?,?,?,?,?,?)",
        (doc_id, "0001", "leaf", "stub", None, None, stub_text))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build kb.sqlite from pipeline artifacts.")
    parser.add_argument("--corpus", metavar="PATH")
    return parser


def run(settings: Settings) -> None:
    settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.sqlite_path)
    schema.ensure_persistent(conn)   # never dropped
    schema.rebuild_content(conn)     # drop + recreate docs/nodes/doc_facets/chunks_fts

    # One adapter entry point now (papertrail migration Step 9 — this used to
    # be TWO: load_papers() above (no has_fulltext/fulltext_source at all,
    # forcing this stage to hardcode "arxiv_preprint" for one bibcode literal
    # and "publisher_pdf" for everyone else) plus a second, undeclared
    # `adapter.load_all_including_stubs()` reached via a bare `from
    # kb.adapters import wasp12b`, bypassing corpus.yaml's metadata_adapter
    # mechanism. The adapter now always returns every known entry with
    # has_fulltext/fulltext_source computed correctly, so both the full-doc
    # and stub-doc loops below just read them.
    papers = settings.corpus.load_papers()
    papers_by_id = {p["doc_id"]: p for p in papers}
    section_files = {f.stem for f in settings.sections_dir.glob("*.json")}

    full_count = 0
    for doc_id in tqdm(sorted(section_files), unit="paper", desc="Indexing full-text papers"):
        paper = papers_by_id.get(doc_id)
        if paper is None:
            continue
        cls = load_classification(settings, doc_id)
        fulltext_source = paper.get("fulltext_source") or "publisher_pdf"
        insert_full_doc(conn, settings, doc_id, paper, cls, fulltext_source)
        full_count += 1

    stub_count = 0
    for entry in papers:
        if entry.get("has_fulltext"):
            continue  # already indexed above, or will be once Stages A-D catch up
        if entry["doc_id"] in section_files:
            continue  # defensive: don't double-insert if it actually has a section tree
        insert_stub_doc(conn, entry)
        stub_count += 1

    conn.execute("INSERT INTO chunks_fts (rowid, text) SELECT id, text FROM nodes")
    conn.commit()

    n_docs = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    n_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    n_facets = conn.execute("SELECT COUNT(*) FROM doc_facets").fetchone()[0]
    print(f"\nIndexed: {full_count} full-text papers + {stub_count} stub docs "
          f"= {n_docs} docs, {n_nodes} nodes, {n_facets} facet rows")
    conn.close()


def main(argv=None) -> None:
    args = build_arg_parser().parse_args(argv)
    settings = load_settings(args.corpus, create_dirs=True)
    run(settings)


if __name__ == "__main__":
    main()
