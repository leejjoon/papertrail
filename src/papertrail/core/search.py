"""FTS5/BM25 search over kb.sqlite's `nodes` table.

Merged in papertrail migration Step 7: this used to be one of two
hand-synced copies (this one for the web app's search box/chat, another in
kb/mcp_server.py for the MCP `search_fulltext`/`list_papers` tools) —
the doc_facets schema change forced editing both anyway, so `fts_escape`/
`paper_filters_sql`/`run_search`/`format_hits` now live here once, imported
by both consumers instead of duplicated.

`search_nodes()` (web/chat path — no facet filters, richer per-row shape
including full_text/parent_title for excerpt building) and `run_search()`
(MCP path — facet-filterable, terser per-row shape for markdown rendering)
stay as two functions because they genuinely serve different callers with
different needs; only the pieces that were byte-identical or
schema-coupled got merged.
"""
from __future__ import annotations

import re
import sqlite3

from ..config import Settings


def fts_escape(query: str) -> str:
    """Quote each term so user input can't break FTS5 syntax; OR-join so bm25
    ranks best-covered chunks first without requiring every term. (Hangul in
    the char class is vestigial for this corpus — kept for adoption to a
    corpus that has any.)"""
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


def paper_filters_sql(year=None, **facet_filters) -> tuple[str, list]:
    """Build a ` AND EXISTS (...)`-per-filter clause against doc_facets —
    replaces the pre-Step-7 `d.research_theme_json LIKE '%"value"%'` style,
    which matched substrings and could collide two different facets sharing
    a value name. Facet names are now caller-supplied kwargs (papertrail
    migration Step 9's taxonomy-driven MCP tool signatures), not literals —
    any facet corpus.yaml declares works here with no code change."""
    clauses, params = [], []
    for facet, value in facet_filters.items():
        if value:
            clauses.append("EXISTS (SELECT 1 FROM doc_facets f "
                          "WHERE f.doc_id = d.doc_id AND f.facet = ? AND f.value = ?)")
            params.extend([facet, value])
    if year is not None:
        clauses.append("d.year = ?")
        params.append(year)
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def run_search(db: sqlite3.Connection, query: str, limit: int, **filters) -> list[sqlite3.Row]:
    where, params = paper_filters_sql(**filters)
    return db.execute(
        f"""SELECT n.doc_id, n.node_id, n.block_type, d.title, d.year,
                   snippet(chunks_fts, 0, '**', '**', ' … ', 40) AS snip,
                   bm25(chunks_fts) AS score
            FROM chunks_fts
            JOIN nodes n ON n.id = chunks_fts.rowid
            JOIN docs d ON d.doc_id = n.doc_id
            WHERE chunks_fts MATCH ?{where}
            ORDER BY score LIMIT ?""",
        [fts_escape(query), *params, limit]).fetchall()


def format_hits(hits) -> str:
    if not hits:
        return "No results."
    lines = []
    for h in hits:
        block_type = h["block_type"] or "heading"  # containers have no block_type
        lines.append(f"### {h['doc_id']} ({h['year']}) — {h['title']}\n"
                     f"*node {h['node_id']} / {block_type}* (score {h['score']:.2f})\n"
                     f"{h['snip']}")
    return "\n\n".join(lines)
