#!/usr/bin/env python3
"""MCP server exposing the WASP-12b knowledge base.

Adapted from kasi_papers_analysis/kb/mcp_server.py's read-tool patterns
(get_db, fts_escape, run_search, discovery-by-error), extended with
paragraph/table/figure-level get_section, get_figure_image, and the two
authoring tools (validate_answer/publish_answer) that make this KB's answer
payload contract usable by a coding agent — see docs/spec/04_mcp.md.

Transport chosen by MCP_TRANSPORT (default "stdio"); "http"/"streamable-http"
runs a long-running Streamable-HTTP service (MCP_HOST/MCP_PORT).

Tools:
    search_fulltext    FTS5/BM25 over paragraph/table/figure/equation/reference nodes
    get_paper          metadata + wiki page for one doc_id
    get_section        one node — a whole subtree if it's a heading, or a single
                       leaf (+ its parent heading for context) if not
    get_fulltext       progressive disclosure: whole doc, or a heading index + preview
    read_wiki          any wiki page by relative name
    list_papers        filterable paper table; no args -> available taxonomy values
    get_figure_image   the actual image for a figure node
    validate_answer    check an answer payload's citations resolve (strict)
    publish_answer     validate + store a payload, returns {id, url}
"""
import base64
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

# The installed `mcp` package (2.x) ships MCPServer under mcp.server.mcpserver,
# not the mcp.server.fastmcp.FastMCP path kasi_papers_analysis's reference
# implementation used against an older SDK version — the .tool()-decorator
# and .run() shape carried over, but host/port/stateless_http moved from the
# constructor to run()'s kwargs (verified against the installed package directly).
from mcp.server.mcpserver import Image, MCPServer

sys.path.insert(0, str(Path(__file__).parent.parent))
from kb import config

# webapp/app/answer.py has no FastAPI dependency (by design, matching the
# navigator's FastAPI-free core) — reuse it directly rather than duplicate the
# citation-validation logic.
sys.path.insert(0, str(Path(__file__).parent.parent / "webapp"))
from app.answer import InvalidCitationsError, validate_and_enrich
from app.registry import CorpusRegistry

_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")
_HTTP = _TRANSPORT in ("http", "streamable-http")
mcp = MCPServer("wasp12b-kb")

NO_INDEX_MSG = "Search index not found — run kb/build_index.py first."

_registry = None  # lazy: only built on first authoring-tool call


def get_registry() -> CorpusRegistry:
    global _registry
    if _registry is None:
        _registry = CorpusRegistry()
    return _registry


def get_db() -> sqlite3.Connection | None:
    if not config.KB_SQLITE.exists():
        return None
    db = sqlite3.connect(f"file:{config.KB_SQLITE}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def fts_escape(query: str) -> str:
    """Quote each term so user input can't break FTS5 syntax; OR-join so bm25
    ranks best-covered chunks first without requiring every term. (Hangul in
    the char class is vestigial for this corpus — kept for adoption to a
    corpus that has any, per kasi_papers_analysis's exact function.)"""
    terms = re.findall(r"[\w가-힣.-]+", query)
    return " OR ".join(f'"{t}"' for t in terms) if terms else '""'


def canonical_doc_id(db: sqlite3.Connection, doc_id: str) -> str | None:
    row = db.execute("SELECT doc_id FROM docs WHERE doc_id = ?", (doc_id,)).fetchone()
    if row:
        return row["doc_id"]
    safe = doc_id.replace("&", "+")
    row = db.execute("SELECT doc_id FROM docs WHERE doc_id = ?", (safe,)).fetchone()
    return row["doc_id"] if row else None


def paper_filters_sql(research_theme=None, method=None, facility=None,
                      role_of_wasp12b=None, year=None) -> tuple[str, list]:
    clauses, params = [], []
    if research_theme:
        clauses.append("d.research_theme_json LIKE ?")
        params.append(f'%"{research_theme}"%')
    if method:
        clauses.append("d.method_json LIKE ?")
        params.append(f'%"{method}"%')
    if facility:
        clauses.append("d.facility_json LIKE ?")
        params.append(f'%"{facility}"%')
    if role_of_wasp12b:
        clauses.append("d.role_of_wasp12b = ?")
        params.append(role_of_wasp12b)
    if year is not None:
        clauses.append("d.year = ?")
        params.append(year)
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def run_search(db, query: str, limit: int, **filters) -> list[sqlite3.Row]:
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


@mcp.tool()
def search_fulltext(query: str, limit: int = 10, research_theme: str | None = None,
                    method: str | None = None, facility: str | None = None,
                    role_of_wasp12b: str | None = None) -> str:
    """Keyword search (FTS5/BM25) over every paper's paragraphs, tables, figure
    captions, equations, and bibliography entries. Returns hits with the exact
    (doc_id, node_id) each matched, so a result can be cited directly. Optional
    taxonomy filters: research_theme, method, facility, role_of_wasp12b — call
    list_papers() with no arguments to see valid values."""
    db = get_db()
    if db is None:
        return NO_INDEX_MSG
    try:
        return format_hits(run_search(db, query, limit, research_theme=research_theme,
                                      method=method, facility=facility,
                                      role_of_wasp12b=role_of_wasp12b))
    finally:
        db.close()


@mcp.tool()
def get_paper(doc_id: str) -> str:
    """Metadata and generated wiki page for one paper, by doc_id (its
    safe_bibcode, e.g. "2013Icar..225..432S" or "2009A+A...506..399L")."""
    db = get_db()
    if db is None:
        return NO_INDEX_MSG
    try:
        dc = canonical_doc_id(db, doc_id)
        if dc is None:
            return f"Unknown doc_id: {doc_id}"
        row = db.execute("SELECT * FROM docs WHERE doc_id = ?", (dc,)).fetchone()
    finally:
        db.close()
    meta = {k: row[k] for k in row.keys()}
    for k in ("research_theme_json", "method_json", "facility_json", "authors_json"):
        meta[k.removesuffix("_json")] = json.loads(meta.pop(k)) if meta[k] else None
    meta["has_fulltext"] = bool(meta["has_fulltext"])
    out = "```yaml\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "```\n"
    if not row["has_fulltext"]:
        out += "\n(No full text — this entry is metadata-only; see docs/spec/00_overview.md " \
               "'Corpus completeness'. Nothing beyond title/authors/year is citable.)"
        return out
    wiki = config.WIKI_PAPERS_DIR / f"{dc}.md"
    if wiki.exists():
        out += "\n" + wiki.read_text(encoding="utf-8")
    else:
        out += "\n(No wiki page generated yet — run kb/paper_pages.py.)"
    return out


def _load_section_tree(doc_id: str) -> dict | None:
    path = config.SECTIONS_DIR / f"{doc_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _find_node(nodes: list[dict], node_id: str, parent_title: str | None = None):
    """Returns (node, parent_title) or (None, None)."""
    for n in nodes:
        if n["node_id"] == node_id:
            return n, parent_title
        if n["kind"] == "container":
            found, pt = _find_node(n["nodes"], node_id, n["title"])
            if found is not None:
                return found, pt
    return None, None


def _concat_subtree(node: dict) -> str:
    if node["kind"] == "leaf":
        return node["text"]
    parts = [f"## {node['title']}"]
    for child in node["nodes"]:
        parts.append(_concat_subtree(child))
    return "\n\n".join(parts)


@mcp.tool()
def get_section(doc_id: str, node_id: str) -> str:
    """Read one node of a paper's section tree. If node_id is a heading
    (container), returns that whole subtree's text concatenated. If node_id is
    a leaf (one paragraph/table/figure/equation/footnote/reference), returns
    that node's own content prefixed with its parent heading's title for
    orientation — a single paragraph isn't read fully decontextualized. For a
    table node the raw HTML is returned as-is; for a figure node the caption
    is returned with a pointer to get_figure_image for the actual picture."""
    tree = _load_section_tree(doc_id)
    if tree is None:
        return f"No section tree for {doc_id} (no full text, or doc_id not found)."
    node, parent_title = _find_node(tree["structure"], node_id)
    if node is None:
        return f"Unknown node_id '{node_id}' in {doc_id}."
    if node["kind"] == "container":
        return _concat_subtree(node)
    prefix = f"[{parent_title}] " if parent_title else ""
    body = prefix + node["text"]
    if node["block_type"] == "figure":
        body += "\n\n(This is a figure caption. Call get_figure_image(doc_id, node_id) for the actual image.)"
    return body


@mcp.tool()
def get_fulltext(doc_id: str) -> str:
    """Progressive disclosure of one paper's full text: returns everything if
    short enough, otherwise a heading index (with node_ids) plus a preview,
    so a long paper can be read section by section via get_section."""
    tree = _load_section_tree(doc_id)
    if tree is None:
        return f"No section tree for {doc_id} (no full text, or doc_id not found)."

    def flat_text(nodes, depth=0):
        out = []
        for n in nodes:
            if n["kind"] == "container":
                out.append(f"{'  ' * depth}## {n['title']}  [node {n['node_id']}]")
                out.append(flat_text(n["nodes"], depth + 1))
            else:
                out.append(n["text"])
        return "\n\n".join(out)

    full = flat_text(tree["structure"])
    if len(full) <= 20_000:
        return full

    def heading_index(nodes, depth=0):
        out = []
        for n in nodes:
            if n["kind"] == "container":
                out.append(f"{'  ' * depth}- {n['title']}  [node {n['node_id']}]")
                out.append(heading_index(n["nodes"], depth + 1))
        return "\n".join(o for o in out if o)

    idx = heading_index(tree["structure"])
    return (f"Document is {len(full)} chars. Section index:\n{idx}\n\n"
            f"Call get_section(doc_id, node_id) to read one, or use "
            f"search_fulltext for targeted lookup.\n\n" + full[:8_000] + "\n…")


@mcp.tool()
def read_wiki(page: str = "index") -> str:
    """Read a wiki page by relative name, e.g. "index",
    "topics/theme-orbital-decay", "papers/2013Icar..225..432S"."""
    name = page.removesuffix(".md")
    path = (config.WIKI_DIR / f"{name}.md").resolve()
    if not path.is_relative_to(config.WIKI_DIR.resolve()) or not path.exists():
        pages = sorted(str(p.relative_to(config.WIKI_DIR))[:-3]
                       for p in config.WIKI_DIR.rglob("*.md"))
        return (f"Page '{page}' not found. Available pages:\n"
                + "\n".join(f"- {p}" for p in pages))
    return path.read_text(encoding="utf-8")


@mcp.tool()
def list_papers(research_theme: str | None = None, method: str | None = None,
                facility: str | None = None, role_of_wasp12b: str | None = None,
                year: int | None = None) -> str:
    """List papers matching the filters as a markdown table. Call with no
    arguments to see the available taxonomy values and overall corpus
    coverage (how many of the 210 entries have full text vs. metadata only)."""
    db = get_db()
    if db is None:
        return NO_INDEX_MSG
    try:
        if not any([research_theme, method, facility, role_of_wasp12b, year]):
            themes, methods, facilities, years = set(), set(), set(), set()
            for row in db.execute(
                    "SELECT research_theme_json, method_json, facility_json, year FROM docs"):
                themes.update(json.loads(row["research_theme_json"]))
                methods.update(json.loads(row["method_json"]))
                facilities.update(json.loads(row["facility_json"]))
                if row["year"]:
                    years.add(row["year"])
            total = db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
            full = db.execute("SELECT COUNT(*) FROM docs WHERE has_fulltext = 1").fetchone()[0]
            return (f"{total} papers indexed ({full} with full text, "
                    f"{total - full} metadata-only). Filter values —\n"
                    f"- research_theme: {', '.join(sorted(themes))}\n"
                    f"- method: {', '.join(sorted(methods))}\n"
                    f"- facility: {', '.join(sorted(facilities))}\n"
                    f"- role_of_wasp12b: primary-target, incidental-mention\n"
                    f"- year: {', '.join(map(str, sorted(years)))}")
        where, params = paper_filters_sql(research_theme, method, facility,
                                          role_of_wasp12b, year)
        rows = db.execute(
            f"SELECT * FROM docs d WHERE 1=1{where} ORDER BY d.year, d.doc_id",
            params).fetchall()
    finally:
        db.close()
    if not rows:
        return "No papers match those filters."
    lines = ["| doc_id | Year | Title | Role | Facility | Fulltext |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        facs = ", ".join(json.loads(r["facility_json"]))
        title = (r["title"] or "")[:70]
        lines.append(f"| {r['doc_id']} | {r['year']} | {title} "
                     f"| {r['role_of_wasp12b'] or ''} | {facs} "
                     f"| {'yes' if r['has_fulltext'] else 'no'} |")
    lines.append(f"\n{len(rows)} paper(s).")
    return "\n".join(lines)


@mcp.tool(structured_output=False)  # Image isn't a pydantic-schema-able type —
                                     # this SDK tries to build an output model
                                     # for structured_output by default, which
                                     # fails on Image | str; skip it, this
                                     # tool's return value was never meant to
                                     # be a structured/JSON result anyway.
def get_figure_image(doc_id: str, node_id: str) -> Image | str:
    """The actual picture for a figure node (as returned by search_fulltext or
    get_section). Errors with a message if node_id isn't a figure node."""
    tree = _load_section_tree(doc_id)
    if tree is None:
        return f"No section tree for {doc_id}."
    node, _ = _find_node(tree["structure"], node_id)
    if node is None:
        return f"Unknown node_id '{node_id}' in {doc_id}."
    if node.get("block_type") != "figure":
        return f"Node {node_id} is a '{node.get('block_type')}', not a figure."
    asset = node.get("image_asset")
    if not asset:
        return f"Figure node {node_id} has no extracted image."
    path = config.ASSETS_DIR / asset
    if not path.exists():
        return f"Image asset missing on disk: {asset}"
    return Image(data=path.read_bytes(), format="jpeg")


@mcp.tool()
def validate_answer(payload: dict) -> dict:
    """Check that every citation in an answer payload resolves to a real
    (doc_id, node_id) — strict mode, so any unresolved citation is reported as
    an error rather than silently dropped. Returns the enriched payload on
    success, or a dict with 'error' and 'offending' citations on failure. Note:
    this proves a cited node EXISTS — it does not prove the node's text
    actually supports the claim next to its [n] marker; that is the caller's
    responsibility."""
    registry = get_registry()
    try:
        return validate_and_enrich(payload, registry.docs, strict=True)
    except InvalidCitationsError as exc:
        return {"error": "invalid citations", "offending": exc.offending}
    except ValueError as exc:
        return {"error": str(exc)}


@mcp.tool()
def publish_answer(payload: dict, title: str | None = None) -> dict:
    """Validate and store an answer payload (kind='agent_adhoc', no
    conversation) so the web app displays it with the same citation UX as a
    chat answer. Returns {id, url} on success, or {error, offending} if any
    citation doesn't resolve — fix and retry rather than publish a broken
    payload."""
    registry = get_registry()
    try:
        enriched = validate_and_enrich(payload, registry.docs, strict=True)
    except InvalidCitationsError as exc:
        return {"error": "invalid citations", "offending": exc.offending}
    except ValueError as exc:
        return {"error": str(exc)}

    answer_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db = sqlite3.connect(config.KB_SQLITE)
    try:
        db.execute(
            "INSERT INTO answers (id, kind, conversation_id, title, payload_json, created_at) "
            "VALUES (?, 'agent_adhoc', NULL, ?, ?, ?)",
            (answer_id, title, json.dumps(enriched, ensure_ascii=False), now))
        db.commit()
    finally:
        db.close()
    base_url = os.getenv("WEBAPP_BASE_URL", "http://127.0.0.1:8000")
    return {"id": answer_id, "url": f"{base_url}/#answer={answer_id}"}


if __name__ == "__main__":
    if _HTTP:
        # For HTTP: bind host/port and run stateless (each request independent
        # — fits read-only tools + many short-lived clients, no session
        # bookkeeping), matching kasi_papers_analysis's rationale.
        mcp.run(transport="streamable-http",
               host=os.getenv("MCP_HOST", "0.0.0.0"),
               port=int(os.getenv("MCP_PORT", "8000")),
               stateless_http=True)
    else:
        mcp.run(transport="stdio")
