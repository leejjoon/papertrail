# MCP Server

`kb/mcp_server.py` — 9 tools over the same `kb.sqlite`/`kb/wiki/` artifacts the web app
serves, so an agent and a human chatting see identical content and an agent-published
answer renders with the identical citation UX. Following
`kasi_papers_analysis/kb/mcp_server.py`'s shape and helpers directly.

**Corrections:** the "Built on `mcp.server.fastmcp.FastMCP`" claim above was never true
of the actual `kb/mcp_server.py` — it uses `mcp.server.mcpserver.MCPServer` (the
installed SDK is `mcp==2.0.0`; `FastMCP`'s decorator-based API is the 1.x shape). Kept
here uncorrected in the transport section below only insofar as it still shows the real
`MCPServer` usage; the prose above is the stale bit. Separately, as of the `papertrail`
extraction (`CLAUDE.md`, `05_adoption.md`), `kb/mcp_server.py` no longer exists — it was
replaced by `src/papertrail/mcp/server.py`'s `build_server(settings)`, which defines all
9 tools as closures over an explicit `settings` object (no module-global config, no
`sys.path` injection) and additionally builds `search_fulltext`/`list_papers`'s facet
parameters (`research_theme`/`method`/`facility`/`role_of_wasp12b` below) **dynamically
from `corpus.yaml`'s taxonomy** at server-start time via a runtime `inspect.Signature`,
rather than the hardcoded parameter list this document shows — see `05_adoption.md`
step 3. The 9 tool names, their behavior, and (for wasp12b specifically) their effective
parameters are otherwise unchanged; not yet the live process.

## Shared helpers (reused from kasi almost verbatim)

- `get_db()` — opens `kb.sqlite` via `file:{KB_SQLITE}?mode=ro` URI, `row_factory =
  sqlite3.Row`. Returns `None` (tools reply "run the pipeline first") if the file
  doesn't exist — read-only open means the MCP server can never corrupt the index even
  under a bug.
- `fts_escape(q)` — kasi's exact function: `re.findall(r"[\w가-힣.-]+", q)`, OR-join as
  quoted terms. (Hangul in the char class is vestigial for this corpus — no Korean
  content here — kept for `05_adoption.md` corpora that might have any, and because
  it's zero-cost to keep.)
- `run_search(query, filters, limit)` — the one FTS query, `bm25()`-ordered,
  `snippet(chunks_fts, 0, '**', '**', ' … ', 40)`. Filters translate to `AND` clauses on
  `docs.research_theme_json LIKE '%"<value>"%'` etc. (kasi's `paper_filters_sql`
  pattern, generalized to this corpus's 4 taxonomy facets instead of kasi's
  technique/subfield/tier).
- Writes (only used by `validate_answer`/`publish_answer`) open a **separate**
  read-write connection — the read tools' connection stays read-only throughout the
  process lifetime.

## Read tools

### `search_fulltext(query: str, limit: int = 10, research_theme: str | None = None, method: str | None = None, facility: str | None = None, role_of_wasp12b: str | None = None) -> str`
FTS5/BM25 over `nodes.text`. Returns markdown hit list; each hit shows
`(doc_id, node_id)`, the paper title/year, `block_type`, and the BM25 snippet — exactly
what a caller needs to turn into a citation:
```
### 2017ApJ...835...16C (2017) — Orbital decay of WASP-12b
*node 0042 / paragraph* (score -4.2)
...period derivative of **dP/dt ≈ -29 ms/yr**, consistent with tidal dissipation...
```

### `get_paper(doc_id: str) -> str`
Metadata header (yaml-fenced, taxonomy facets de-JSON'd) + the full wiki page markdown
(`kb/wiki/papers/<doc_id>.md`), if one exists. For a stub doc (`has_fulltext=false`),
returns just the metadata header with a note that no full text/wiki page exists.

### `get_section(doc_id: str, node_id: str) -> str`
Context-aware, unlike kasi's flat `get_fulltext(section=...)`:
- `node_id` resolves to a **container** → concatenate all descendant leaf nodes' text
  in document order (i.e. "give me all of §4 RESULTS").
- `node_id` resolves to a **leaf** → return that node's own content, prefixed with its
  parent container's title (e.g. `[§3. RESULTS] <paragraph text>`), so a single
  paragraph/table/figure isn't read fully out of context. For `block_type=table`, the
  raw HTML is returned as-is (the caller/agent renders or reasons over it directly);
  for `block_type=figure`, the caption text is returned with a note pointing at
  `get_figure_image` for the actual picture.

### `get_fulltext(doc_id: str) -> str`
Kasi's progressive-disclosure pattern, generalized: if the paper's total text is short
enough (~20,000 chars), return it all; otherwise return a heading index (every
container's title + node_id) plus the first ~8,000 chars, telling the caller to come
back with `get_section` for a specific part.

### `read_wiki(page: str = "index") -> str`
Serves `kb/wiki/**/*.md`. On a miss, returns the full list of valid page names
(`rglob("*.md")`, `.md` stripped) — kasi's discovery-by-error pattern, which is also
literally the argument form to pass back in, so an agent can browse the wiki with zero
out-of-band documentation.

### `list_papers(research_theme=None, method=None, facility=None, role_of_wasp12b=None, year=None) -> str`
Zero-arg call = schema introspection: scans `docs` and returns every valid value for
each of the 4 taxonomy facets + year range + total counts (kasi's "zero-arg call teaches
the filter vocabulary" pattern) — including how many of the 210 total entries are stubs
vs. full-text, so an agent immediately understands the corpus's actual coverage. With
filters: a markdown table of matching papers.

### `get_figure_image(doc_id: str, node_id: str) -> Image`
New tool (not in kasi). Resolves `node_id` to a `figure`-typed leaf node, reads
`kb/data/assets/<doc_id>/<...>.jpeg`, returns it as an MCP image content block (FastMCP's
`Image` return type — base64-encoded, so it displays inline in a client that supports
image content, same as any other MCP tool returning an image). Errors clearly if
`node_id` isn't a figure node, or if (edge case, shouldn't happen post-Stage-A) the
asset file is missing.

## Authoring tools

### `validate_answer(payload: dict) -> dict`
Wraps `webapp/app/answer.py`'s `validate_and_enrich(payload, strict=True)` (imported
directly — `kb/mcp_server.py` adds `webapp/` to `sys.path`, same cross-module import
style `01b_fetch_pdfs_browser.py` uses for `01_fetch_pdfs.py` today). Returns either the
enriched payload (all citations resolved) or a structured error list — the same
`{result_index, id, doc_id, node_id, reason}` shape the navigator's `POST /api/answer`
returns on a 422, so a single error-handling code path serves both the API and this
tool.

### `publish_answer(payload: dict, title: str | None = None) -> dict`
Calls `validate_answer` internally (an agent shouldn't be able to publish something
that fails validation — no lenient path here, unlike chat's `strict=false`), then
inserts into `answers` (`kind="agent_adhoc"`, `conversation_id=NULL`). Returns
`{id, url}` where `url` is the web app's `#answer=<id>` deep link. **No push** — the
agent is responsible for telling its own user the URL (this is the standard
"here's a link" pattern already used elsewhere, e.g. how a `paper_download`-style
background task reports its result).

## Agent authoring workflow

Directly modeled on `multi_doc_navigator`'s `generate-citation-payload` skill
(`corpus.py` search/read + `validate_payload.py --resolve` in a loop):

1. `list_papers()` or `search_fulltext(question)` to scope which papers are relevant.
2. `get_section`/`get_paper`/`get_figure_image` to actually read the content the answer
   will cite — **never cite a `(doc_id, node_id)` you have not called one of these
   tools on**.
3. Draft the answer prose with `[n]` markers and a matching `citations[]` list.
4. `validate_answer(payload)` — loop until it returns cleanly (fix unresolved
   citations, mismatched ids, missing structural fields).
5. `publish_answer(payload, title="...")` — report the returned `url` to the user.

Same caveat as the navigator's skill, carried over verbatim because it's still exactly
true here: **`validate_answer` proves a cited node *exists* — it does not prove the
node's text actually *supports* the claim next to its `[n]` marker.** That correctness
is the agent's responsibility; validation only catches broken references, not
unsupported ones.

## Transport & deployment

Env-switched transport, kasi's exact pattern:
```python
_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")
_HTTP = _TRANSPORT in ("http", "streamable-http")
mcp = FastMCP("wasp12b-kb", **({"host": ..., "port": ..., "stateless_http": True} if _HTTP else {}))
mcp.run(transport="streamable-http" if _HTTP else "stdio")
```

Local/venv usage (`.mcp.json`):
```json
{"mcpServers": {"wasp12b-kb": {"command": ".venv/bin/python", "args": ["kb/mcp_server.py"]}}}
```

`papertrail`'s equivalent, what `.mcp.json` actually contains as of this writing (see
`05_adoption.md` step 9 — the process this launches is `src/papertrail/mcp/server.py`,
not `kb/mcp_server.py`, which no longer exists):
```json
{"mcpServers": {"wasp12b-kb": {"command": ".venv/bin/papertrail", "args": ["mcp"],
                               "env": {"PAPERTRAIL_CORPUS": "kb"}}}}
```
(`PAPERTRAIL_CORPUS` here still points at `kb/`, the live corpus directory, pending
cutover to `corpora/wasp12b/` — see `CLAUDE.md`'s mid-migration section.)

Optional Docker (P4, if ever needed for remote access): kasi's exact ro-mount pattern —
image contains only `config.py` + `mcp_server.py` + a minimal `requirements-mcp.txt`
(`mcp`, `openai`, `pyyaml`, `python-dotenv` — no PDF/image-processing deps needed at
serve time), `data/` and `wiki/` mounted read-only, never baked into the image (keeps
the image small, avoids shipping paper full-text/images inside a distributable
artifact).
