#!/usr/bin/env python3
"""Stage E — build kb.sqlite.

Full drop-and-rebuild of the content tables (docs, nodes, chunks_fts) every run
— no resume logic needed at this scale (kasi_papers_analysis's precedent).
answers/conversations/messages are created once and NEVER dropped by a rebuild,
so re-running the pipeline after adding papers doesn't destroy chat history.

Nodes are indexed directly from kb/data/sections/*.json — one row per leaf AND
container node — with no resplitting step (unlike kasi's kb, nodes here are
already paragraph/table/figure-sized, so each is already a natural FTS chunk).
Wiki page bodies (paper/topic pages) are NOT separately indexed: their prose
already exists as the primary-source node(s) it cites elsewhere in `nodes`, and
indexing it twice would let two rows answer the same search hit with different
provenance. Wiki pages remain browsable independent of the FTS index.

The 20 stub docs (no fulltext — see docs/spec/00_overview.md) get one node row
each of block_type='stub' so they're findable by title/author search.

Usage:
    python3 kb/build_index.py
"""
import json
import sqlite3
import sys
from pathlib import Path

import yaml
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))
from kb import config
from kb.adapters import wasp12b as adapter

SCHEMA_CONTENT = """
CREATE TABLE docs (
    doc_id TEXT PRIMARY KEY,
    title TEXT, year INTEGER, journal TEXT, authors_json TEXT,
    fulltext_source TEXT,
    has_fulltext INTEGER NOT NULL,
    research_theme_json TEXT, method_json TEXT, facility_json TEXT,
    role_of_wasp12b TEXT
);

CREATE TABLE nodes (
    id INTEGER PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES docs(doc_id),
    node_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    block_type TEXT,
    parent_title TEXT,
    page INTEGER,
    text TEXT NOT NULL,
    UNIQUE (doc_id, node_id)
);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
    text, content='nodes', content_rowid='id', tokenize='porter unicode61'
);
"""

SCHEMA_PERSISTENT = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS answers (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    conversation_id INTEGER REFERENCES conversations(id),
    title TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL,
    text TEXT,
    answer_id TEXT REFERENCES answers(id),
    created_at TEXT NOT NULL
);
"""

CONTENT_TABLES = ["chunks_fts", "chunks_fts_data", "chunks_fts_idx",
                   "chunks_fts_docsize", "chunks_fts_config", "nodes", "docs"]


def load_classification(doc_id: str) -> dict:
    path = config.CLASSIFICATION_DIR / f"{doc_id}.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def insert_full_doc(conn: sqlite3.Connection, doc_id: str, paper: dict, cls: dict,
                     fulltext_source: str) -> None:
    conn.execute(
        "INSERT INTO docs (doc_id, title, year, journal, authors_json, "
        "fulltext_source, has_fulltext, research_theme_json, method_json, "
        "facility_json, role_of_wasp12b) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (doc_id, paper.get("title"), paper.get("year"), paper.get("journal"),
         json.dumps(paper.get("authors")), fulltext_source, 1,
         json.dumps(cls.get("research_theme") or []),
         json.dumps(cls.get("method") or []),
         json.dumps(cls.get("facility") or []),
         cls.get("role_of_wasp12b")))

    section_tree = json.loads((config.SECTIONS_DIR / f"{doc_id}.json").read_text(encoding="utf-8"))

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
        "fulltext_source, has_fulltext, research_theme_json, method_json, "
        "facility_json, role_of_wasp12b) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (doc_id, entry.get("title"), entry.get("year"), entry.get("journal"),
         json.dumps(entry.get("authors")), None, 0, "[]", "[]", "[]", None))
    authors = entry.get("authors") or ""
    stub_text = f"{entry.get('title', '')}. {authors}. {entry.get('journal', '')} ({entry.get('year', '')})."
    conn.execute(
        "INSERT INTO nodes (doc_id, node_id, kind, block_type, parent_title, "
        "page, text) VALUES (?,?,?,?,?,?,?)",
        (doc_id, "0001", "leaf", "stub", None, None, stub_text))


def main() -> None:
    config.KB_SQLITE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.KB_SQLITE)
    conn.executescript(SCHEMA_PERSISTENT)  # never dropped

    for table in CONTENT_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(SCHEMA_CONTENT)

    papers_by_id = {p["doc_id"]: p for p in config.load_papers()}
    section_files = {f.stem for f in config.SECTIONS_DIR.glob("*.json")}

    full_count = 0
    for doc_id in tqdm(sorted(section_files), unit="paper", desc="Indexing full-text papers"):
        paper = papers_by_id.get(doc_id)
        if paper is None:
            continue
        cls = load_classification(doc_id)
        fulltext_source = "arxiv_preprint" if doc_id == "2010RPPh...73a6901B" else "publisher_pdf"
        insert_full_doc(conn, doc_id, paper, cls, fulltext_source)
        full_count += 1

    stub_count = 0
    for entry in adapter.load_all_including_stubs():
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
    print(f"\nIndexed: {full_count} full-text papers + {stub_count} stub docs "
          f"= {n_docs} docs, {n_nodes} nodes")
    conn.close()


if __name__ == "__main__":
    main()
