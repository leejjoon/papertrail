"""FTS5/BM25 search over kb.sqlite's `nodes` table.

Same conventions as kb/mcp_server.py's search helpers (kept in sync
deliberately, not shared by import, since the MCP server and this web app are
independently deployable processes that only agree via the sqlite file) —
merging the two hand-synced copies is Step 7 of the papertrail migration,
alongside the doc_facets schema change that forces editing both anyway.
"""
from __future__ import annotations

import re
import sqlite3

from ..config import Settings


def fts_escape(query: str) -> str:
    terms = re.findall(r"[\w가-힣.-]+", query)
    return " OR ".join(f'"{t}"' for t in terms) if terms else '""'


def search_nodes(settings: Settings, query: str, limit: int = 12) -> list[dict]:
    conn = sqlite3.connect(f"file:{settings.sqlite_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT n.doc_id, n.node_id, n.block_type, n.parent_title, d.title, d.year,
                      snippet(chunks_fts, 0, '**', '**', ' … ', 40) AS snip,
                      n.text AS full_text,
                      bm25(chunks_fts) AS score
               FROM chunks_fts
               JOIN nodes n ON n.id = chunks_fts.rowid
               JOIN docs d ON d.doc_id = n.doc_id
               WHERE chunks_fts MATCH ? AND d.has_fulltext = 1
               ORDER BY score LIMIT ?""",
            (fts_escape(query), limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
