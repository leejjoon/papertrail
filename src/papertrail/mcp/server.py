#!/usr/bin/env python3
"""MCP server exposing a papertrail knowledge base.

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

Every tool is defined inside build_server(settings) rather than at module
level: a `settings` parameter on a @mcp.tool()-decorated function would leak
into the tool's JSON schema, so closing over it is the only way to give
every tool one shared, explicit corpus configuration instead of each reading
a module-global. This is also what kills this file's former split-brain bug:
before, `get_db()`/`_load_section_tree()` read `kb.config` (hardcoded paths,
no env override) while `get_registry()` -> `CorpusRegistry()` read
`webapp.app.config` (env-override-aware) — setting KB_SQLITE made the
search tools and validate_answer/publish_answer silently see two different
corpora. Now every tool shares the one `settings` this module was built
with.
"""
import argparse
import inspect
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

import yaml
from pydantic import Field

# The installed `mcp` package (2.x) ships MCPServer under mcp.server.mcpserver,
# not the mcp.server.fastmcp.FastMCP path kasi_papers_analysis's reference
# implementation used against an older SDK version — the .tool()-decorator
# and .run() shape carried over, but host/port/stateless_http moved from the
# constructor to run()'s kwargs (verified against the installed package directly).
from mcp.server.mcpserver import Image, MCPServer

from ..config import Settings, load_settings
from ..core.citations import InvalidCitationsError, validate_and_enrich
from ..core.nodes import context_window
from ..core.registry import CorpusRegistry
from ..core.search import format_hits, paper_filters_sql, run_search
from ..identity import CitationStyle, load_citation_style
from ..taxonomy import FacetSpec

NO_INDEX_MSG = "Search index not found — run `papertrail index` first."

# Above this many values, a facet's parameter degrades from a JSON-Schema
# enum (Literal[...]) to a plain string with the vocabulary spelled out in
# the description — an enum with hundreds of values makes for an unreadable
# tool schema.
MAX_ENUM_VALUES = 30


def _facet_params(taxonomy: dict[str, FacetSpec]) -> list[inspect.Parameter]:
    """One optional keyword-only parameter per taxonomy facet, each a real
    JSON-Schema enum of its values — this is what replaces the hardcoded
    `research_theme: str | None = None, method: ...` parameter lists
    search_fulltext/list_papers used to declare. Verified against the
    installed mcp==2.0.0 (papertrail migration Step 9): a manually assigned
    __signature__/__annotations__ registers correctly via MCPServer.add_tool()
    and produces a real enum in the tool's JSON schema; an out-of-vocabulary
    value is rejected with a pydantic ValidationError surfaced as a ToolError,
    not silently ignored."""
    params = []
    for facet, spec in taxonomy.items():
        if spec.values and len(spec.values) <= MAX_ENUM_VALUES:
            ann = Annotated[Literal[tuple(spec.values)] | None,
                            Field(description=f"filter by {spec.label.lower()}")]
        else:
            values_hint = f" — one of: {', '.join(spec.values)}" if spec.values else ""
            ann = Annotated[str | None, Field(description=f"filter by {spec.label.lower()}{values_hint}")]
        params.append(inspect.Parameter(facet, inspect.Parameter.KEYWORD_ONLY, default=None, annotation=ann))
    return params


def _bind_signature(fn, params: list[inspect.Parameter], return_annotation, doc: str):
    """Give `fn` a runtime-built signature (base params + one per taxonomy
    facet) — must happen BEFORE mcp.add_tool(fn, ...), since that's what
    inspects fn's signature to build the tool's JSON schema. This is why
    these tools are registered via add_tool() rather than the @mcp.tool()
    decorator syntax: the decorator runs at function-definition time, before
    there's a chance to rewrite the signature."""
    fn.__signature__ = inspect.Signature(params, return_annotation=return_annotation)
    fn.__annotations__ = {p.name: p.annotation for p in params} | {"return": return_annotation}
    fn.__doc__ = doc
    return fn


def canonical_doc_id(db: sqlite3.Connection, doc_id: str, style: CitationStyle) -> str | None:
    """Try doc_id as given, then each of style.canonicalize()'s alternate
    encodings (e.g. wasp12b's '&' -> '+' bibcode escaping) — replaces the
    hardcoded doc_id.replace('&', '+') this used to do unconditionally."""
    for candidate in style.canonicalize(doc_id):
        row = db.execute("SELECT doc_id FROM docs WHERE doc_id = ?", (candidate,)).fetchone()
        if row:
            return row["doc_id"]
    return None


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


def build_server(settings: Settings) -> MCPServer:
    mcp = MCPServer(f"{settings.corpus.name}-kb")
    style = load_citation_style(settings.corpus)
    _registry: dict[str, CorpusRegistry] = {}  # lazy box: only built on first authoring-tool call

    def get_registry() -> CorpusRegistry:
        if "r" not in _registry:
            _registry["r"] = CorpusRegistry(settings)
        return _registry["r"]

    def get_db() -> sqlite3.Connection | None:
        if not settings.sqlite_path.exists():
            return None
        db = sqlite3.connect(f"file:{settings.sqlite_path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        return db

    def _load_section_tree(doc_id: str) -> dict | None:
        path = settings.sections_dir / f"{doc_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    taxonomy = settings.corpus.taxonomy

    def _search_fulltext(query: str, limit: int = 10, **filters) -> str:
        db = get_db()
        if db is None:
            return NO_INDEX_MSG
        try:
            return format_hits(run_search(db, query, limit, **filters))
        finally:
            db.close()

    facet_names = ", ".join(taxonomy) or "(none declared)"
    _bind_signature(
        _search_fulltext,
        [inspect.Parameter("query", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str),
         inspect.Parameter("limit", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=10, annotation=int),
         *_facet_params(taxonomy)],
        str,
        "Keyword search (FTS5/BM25) over every paper's paragraphs, tables, figure "
        "captions, equations, and bibliography entries. Returns hits with the exact "
        "(doc_id, node_id) each matched, so a result can be cited directly. Optional "
        f"taxonomy filters: {facet_names} — call list_papers() with no arguments to "
        "see valid values.",
    )
    mcp.add_tool(_search_fulltext, name="search_fulltext", description=_search_fulltext.__doc__)

    @mcp.tool()
    def get_paper(doc_id: str) -> str:
        """Metadata and generated wiki page for one paper, by doc_id (its
        safe_bibcode, e.g. "2013Icar..225..432S" or "2009A+A...506..399L")."""
        db = get_db()
        if db is None:
            return NO_INDEX_MSG
        try:
            dc = canonical_doc_id(db, doc_id, style)
            if dc is None:
                return f"Unknown doc_id: {doc_id}"
            row = db.execute("SELECT * FROM docs WHERE doc_id = ?", (dc,)).fetchone()
        finally:
            db.close()
        meta = {k: row[k] for k in row.keys()}
        meta["authors"] = json.loads(meta.pop("authors_json")) if meta["authors_json"] else None
        meta.update(json.loads(meta.pop("facets_json") or "{}"))
        meta["extra"] = json.loads(meta.pop("extra_json") or "{}")
        meta["has_fulltext"] = bool(meta["has_fulltext"])
        out = "```yaml\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "```\n"
        if not row["has_fulltext"]:
            out += "\n(No full text — this entry is metadata-only; see docs/spec/00_overview.md " \
                   "'Corpus completeness'. Nothing beyond title/authors/year is citable.)"
            return out
        wiki = settings.wiki_dir / "papers" / f"{dc}.md"
        if wiki.exists():
            out += "\n" + wiki.read_text(encoding="utf-8")
        else:
            out += "\n(No wiki page generated yet — run `papertrail paper-pages`.)"
        return out

    @mcp.tool()
    def get_section(doc_id: str, node_id: str, context: int = 0) -> str:
        """Read one node of a paper's section tree. If node_id is a heading
        (container), returns that whole subtree's text concatenated. If node_id is
        a leaf (one paragraph/table/figure/equation/footnote/reference), returns
        that node's own content prefixed with its parent heading's title for
        orientation — a single paragraph isn't read fully decontextualized. For a
        table node the raw HTML is returned as-is; for a figure node the caption
        is returned with a pointer to get_figure_image for the actual picture.

        `context`: how many sibling nodes to include before and after, within the
        same section only (default 0 — just the node itself, unchanged from
        before). Use this if the leaf's meaning looks ambiguous read alone — a
        pronoun or comparison ("this discrepancy," "the equilibrium case," a bare
        ratio with no stated assumptions) that points outside the node — by
        re-calling with a larger context instead of guessing. The cited node
        itself is still just this node_id; surrounding text is marked as context
        only, not part of what you'd cite."""
        tree = _load_section_tree(doc_id)
        if tree is None:
            return f"No section tree for {doc_id} (no full text, or doc_id not found)."
        node, _ = _find_node(tree["structure"], node_id)
        if node is None:
            return f"Unknown node_id '{node_id}' in {doc_id}."
        body = context_window(tree["structure"], node_id, context)
        if node.get("block_type") == "figure":
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
        path = (settings.wiki_dir / f"{name}.md").resolve()
        if not path.is_relative_to(settings.wiki_dir.resolve()) or not path.exists():
            pages = sorted(str(p.relative_to(settings.wiki_dir))[:-3]
                           for p in settings.wiki_dir.rglob("*.md"))
            return (f"Page '{page}' not found. Available pages:\n"
                    + "\n".join(f"- {p}" for p in pages))
        return path.read_text(encoding="utf-8")

    table_columns = settings.corpus.navigation.table_columns

    def _list_papers(year: int | None = None, **filters) -> str:
        db = get_db()
        if db is None:
            return NO_INDEX_MSG
        try:
            if year is None and not any(filters.values()):
                # facet, value -> distinct values, one indexed query instead of a
                # full-table scan + per-row json.loads (papertrail migration Step 7).
                facet_values: dict[str, set] = {}
                for row in db.execute("SELECT DISTINCT facet, value FROM doc_facets"):
                    facet_values.setdefault(row["facet"], set()).add(row["value"])
                years = {r["year"] for r in db.execute("SELECT DISTINCT year FROM docs")
                         if r["year"]}
                total = db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
                full = db.execute("SELECT COUNT(*) FROM docs WHERE has_fulltext = 1").fetchone()[0]
                lines = [f"{total} papers indexed ({full} with full text, "
                        f"{total - full} metadata-only). Filter values —"]
                for facet in taxonomy:
                    lines.append(f"- {facet}: {', '.join(sorted(facet_values.get(facet, ())))}")
                lines.append(f"- year: {', '.join(map(str, sorted(years)))}")
                return "\n".join(lines)
            where, params = paper_filters_sql(year=year, **filters)
            rows = db.execute(
                f"SELECT * FROM docs d WHERE 1=1{where} ORDER BY d.year, d.doc_id",
                params).fetchall()
        finally:
            db.close()
        if not rows:
            return "No papers match those filters."
        headers = ["doc_id", "Year", "Title"] + [taxonomy[c].label for c in table_columns] + ["Fulltext"]
        lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
        for r in rows:
            facets = json.loads(r["facets_json"] or "{}")
            title = (r["title"] or "")[:70]
            cells = [r["doc_id"], str(r["year"] or ""), title]
            for col in table_columns:
                v = facets.get(col)
                cells.append(", ".join(v) if isinstance(v, list) else (v or ""))
            cells.append("yes" if r["has_fulltext"] else "no")
            lines.append("| " + " | ".join(cells) + " |")
        lines.append(f"\n{len(rows)} paper(s).")
        return "\n".join(lines)

    _bind_signature(
        _list_papers,
        [inspect.Parameter("year", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=int | None),
         *_facet_params(taxonomy)],
        str,
        "List papers matching the filters as a markdown table. Call with no "
        "arguments to see the available taxonomy values and overall corpus "
        "coverage (how many entries have full text vs. metadata only).",
    )
    mcp.add_tool(_list_papers, name="list_papers", description=_list_papers.__doc__)

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
        path = settings.assets_dir / asset
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
            return validate_and_enrich(payload, registry.docs, registry.style, strict=True)
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
            enriched = validate_and_enrich(payload, registry.docs, registry.style, strict=True)
        except InvalidCitationsError as exc:
            return {"error": "invalid citations", "offending": exc.offending}
        except ValueError as exc:
            return {"error": str(exc)}

        answer_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db = sqlite3.connect(settings.sqlite_path)
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

    return mcp


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the papertrail MCP server.")
    parser.add_argument("--corpus", metavar="PATH")
    parser.add_argument("--transport", choices=("stdio", "http", "streamable-http"))
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    return parser


def main(argv=None) -> None:
    # --corpus, matching every pipeline stage's parent parser — previously
    # absent, so `papertrail mcp --corpus X` silently ignored X and fell back
    # to PAPERTRAIL_CORPUS/KB_DIR/upward-search, serving whatever corpus that
    # resolved to with no error. No create_dirs=True: the MCP server, like the
    # web app, never creates directories (config.py's docstring).
    args = build_arg_parser().parse_args(argv)
    settings = load_settings(args.corpus)
    mcp = build_server(settings)
    transport = args.transport or os.getenv("MCP_TRANSPORT", "stdio")
    if transport in ("http", "streamable-http"):
        # For HTTP: bind host/port and run stateless (each request independent
        # — fits read-only tools + many short-lived clients, no session
        # bookkeeping), matching kasi_papers_analysis's rationale.
        mcp.run(transport="streamable-http",
               host=args.host or os.getenv("MCP_HOST", "127.0.0.1"),
               port=args.port or int(os.getenv("MCP_PORT", "8000")),
               stateless_http=True)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
