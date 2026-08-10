"""Sole owner of kb.sqlite's DDL.

Before this module existed, kb/build_index.py owned 100% of the schema and
webapp/app/store.py owned none — so the web app couldn't bootstrap its own
conversations/answers/messages tables on a fresh corpus that hadn't had
Stage E run yet. `ensure_persistent()` fixes that (idempotent, IF NOT
EXISTS); `rebuild_content()` is what Stage E calls every run.

Facet storage (papertrail migration Step 7): one column per taxonomy facet
(the pre-Step-7 `docs.research_theme_json`/`method_json`/`facility_json`/
`role_of_wasp12b`) is replaced by a generic `doc_facets(doc_id, facet,
value)` table plus a `docs.facets_json` display blob holding the same data
as one dict per doc — any taxonomy works with zero schema codegen. A
single-valued facet (e.g. role_of_wasp12b) still gets exactly one
doc_facets row; this makes the table's shape independent of each facet's
`multi:` flag.
"""
from __future__ import annotations

import sqlite3

CONTENT_DDL = """
CREATE TABLE docs (
    doc_id TEXT PRIMARY KEY,
    title TEXT, year INTEGER, journal TEXT, authors_json TEXT,
    fulltext_source TEXT,
    has_fulltext INTEGER NOT NULL,
    facets_json TEXT NOT NULL DEFAULT '{}',
    native_id TEXT,
    extra_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE doc_facets (
    doc_id TEXT NOT NULL REFERENCES docs(doc_id),
    facet TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (doc_id, facet, value)
);
CREATE INDEX ix_doc_facets_fv ON doc_facets(facet, value);

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

PERSISTENT_DDL = """
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

# Dropped and rebuilt every Stage E run. doc_facets included so a rebuild
# clears stale facet rows along with everything else content-derived.
CONTENT_TABLES = ["chunks_fts", "chunks_fts_data", "chunks_fts_idx",
                   "chunks_fts_docsize", "chunks_fts_config",
                   "doc_facets", "nodes", "docs"]


def ensure_persistent(conn: sqlite3.Connection) -> None:
    """Idempotent: create conversations/answers/messages if they don't
    already exist. Safe to call on every connection open — lets the web app
    (or anything else) work against a corpus whose Stage E has never run."""
    conn.executescript(PERSISTENT_DDL)


def rebuild_content(conn: sqlite3.Connection) -> None:
    """Drop and recreate docs/nodes/doc_facets/chunks_fts. Never touches the
    persistent tables (ensure_persistent handles those, separately, and only
    ever adds — never drops)."""
    for table in CONTENT_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(CONTENT_DDL)
