# Web App

FastAPI backend + the `multi_doc_navigator` SPA pattern, extended with search, chat, and
an answer store. Copied into this repo's `webapp/`, not built into
`multi_doc_navigator` itself (that project's PRD explicitly scopes chat/search out —
see `00_overview.md`).

**Migration note:** this describes the live `webapp/` app, still accurate today. The
`papertrail` extraction (`CLAUDE.md`, `05_adoption.md`) moved `registry.py`/`renderer.py`/
`store.py`/`search.py` to `src/papertrail/core/`, `answer.py` to
`src/papertrail/core/citations.py` (same `validate_and_enrich` contract, generalized —
see `01_data_contracts.md`'s migration note on the `reference_label`/`external_links`
field rename), and `main.py`'s module-level `CorpusRegistry()` into an explicit
`create_app(settings)` factory in `src/papertrail/web/app.py` (`webapp/app/main.py` is
now a 2-line shim: `from papertrail.web.asgi import app`). Not yet the live process.

## Reused seam

`multi_doc_navigator/app/registry.py`, `renderer.py`, `answer.py` carry zero FastAPI
imports — copy them into `webapp/app/` and adapt for this system's node model:

- **`registry.py`** — `load_document_tree()` becomes `load_corpus()`: instead of a
  hand-written `document_tree.json`, the manifest is generated from `kb.sqlite`'s
  `docs` table (grouped by `research_theme`/`facility` for the left-nav tree, mirroring
  the navigator's arbitrary-depth group/leaf structure) plus `corpus.yaml`'s taxonomy
  for group ordering/labels. The 20 stub docs appear in the tree (so they're
  discoverable) but their `Doc` records carry `has_fulltext: false` — the viewer must
  check this before attempting to render a body (no `sections/<doc_id>.json` exists for
  them).
- **`renderer.py`** — `render_node_html()` is adapted for the finer node granularity:
  every leaf node (paragraph/table/figure/equation/footnote/reference) gets its own
  anchor + `data-node-id` (was section-level in the navigator; now every citable unit
  gets one, one level lower than before). Figure nodes render their `image_asset` as an
  `<img>` (was a wikilink-rewrite step in the navigator's ancestor `pdf2json_v2.py`
  pipeline — this system's Stage A already writes plain relative paths, so no rewrite
  step is needed here, only a `src="assets/{doc_id}/{file}"` join).
- **`answer.py`** — `validate_and_enrich()` extended per `01_data_contracts.md`'s answer
  payload schema (`id`, `kind`, `conversation_id`, `created_at`; citation resolution
  also checks `docs.has_fulltext`). This function remains the *single* validation path
  shared by: `POST /api/answers`, `POST /api/chat`'s post-generation step, and
  `kb/mcp_server.py`'s `validate_answer`/`publish_answer` tools (imported directly, not
  duplicated — `webapp/app/answer.py` has no FastAPI dependency, so `kb/mcp_server.py`
  can `sys.path`-import it exactly as `01b_fetch_pdfs_browser.py` imports
  `01_fetch_pdfs.py` today).

**Registry loading fix vs. the navigator**: the navigator loads and renders every
document's full HTML at import time (fine for 14 papers, ~1.4MB). At 190+20 docs, this
spec uses **lazy per-doc rendering** — `registry.py` builds the tree/manifest at
startup (cheap: metadata only) but defers `render_doc_html()` until a document is first
requested, with an in-memory LRU cache (bounded, e.g. 50 docs) and a `POST /api/reload`
endpoint that drops the whole cache + re-reads `kb.sqlite`/`corpus.yaml` (so adding a
paper doesn't require a server restart, unlike the navigator).

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | doc/node counts, corpus name |
| GET | `/api/documents` | tree grouped by facility → theme (or however `corpus.yaml` orders it) |
| GET | `/api/documents/{doc_id}` | full rendered HTML (lazy, per above) |
| GET | `/api/documents/{doc_id}/sections/{node_id}` | one node's rendered content — the fetch a citation chip actually needs (navigator had this but the SPA never used it since whole-doc fetch was cheap enough at 14 docs; here it's the primary path for citation preview) |
| GET | `/api/search?q=` | FTS5/BM25, see below |
| GET | `/api/wiki/{page}` | serves `kb/wiki/**/*.md` rendered, miss → page list (kasi's `read_wiki` discovery-by-error pattern, reused verbatim in the API) |
| POST | `/api/conversations` | `{name}` → `{id}` |
| GET | `/api/conversations` | list, newest first |
| PATCH/DELETE | `/api/conversations/{id}` | rename / delete (cascades messages) |
| POST | `/api/chat` | `{conversation_id, question}` → SSE stream, see below |
| GET | `/api/conversations/{id}/messages` | history for reopening a thread |
| POST | `/api/answers` | validate + enrich + store a payload; `?strict=true\|false` (navigator semantics: default lenient, invalid citations become `warnings[]` not a hard failure) |
| GET | `/api/answers/{id}` | fetch a stored answer for rendering |
| GET | `/api/answers?kind=` | list (for the sidebar; optionally filtered by `kind`) |
| GET | `/assets/{doc_id}/{file}` | figure images, `StaticFiles` mount |
| GET | `/` | the SPA |

No `GET /api/events` / SSE-push channel — dropped per `00_overview.md`'s non-goals.
`POST /api/answers` returning `{id, url}` and the SPA resolving `#answer=<id>` on load
is the entire "agent published something, now show it" mechanism.

## Chat

`POST /api/chat` streams step events exactly like `spherex-chatbot`'s pattern
(`_sse()`/`_SSE_HEADERS`, `{"event":"step","step":...,"status":"start"|"done"}`, one
terminal `{"event":"done", ...enriched payload}`) — **this SSE is for the single
in-flight response's progress bar, unrelated to the cross-tab push mechanism dropped in
`00_overview.md`.**

Flow:
1. `step: history` — condense the thread's prior messages into a short context string
   (last N turns verbatim below a token budget; older turns summarized by `CHAT_MODEL`
   if the thread is long — a cheap one-line call, not per-message).
2. `step: search` — `fts_escape(question)` (kasi's exact function: extract
   `[\w가-힣.-]+` tokens, OR-join as quoted terms) → BM25 query over `chunks_fts`,
   `LIMIT 12`, optionally filtered by facets if the question names one explicitly (e.g.
   "papers using JWST" → `facility` filter, extracted the same lightweight way kasi's
   MCP `list_papers` filters work, not a separate NLU step).
3. `step: rewrite` (conditional) — if fewer than 3 hits, one `CHAT_MODEL` call
   rewriting the question into keywords (kasi's `ask` pattern verbatim), re-search.
4. `step: answer` — `CHAT_MODEL`, system prompt: "answer only from the provided
   `(doc_id, node_id)` excerpts below; every claim needs a `[n]`; if the excerpts don't
   support an answer, say so" + condensed history + the up-to-12 retrieved node texts
   labeled `[n] (doc_id, node_id, parent section title): <text>`.
5. `step: context` (conditional) — a single paragraph-level excerpt is often ambiguous
   read alone (unresolved pronoun/comparison, a paper-specific term undefined in that
   node); rather than depend on this gateway's unverified tool-calling support, the model
   may return `{"need_context": [{doc_id, node_id, context}]}` instead of a final answer.
   If so, widen up to 4 of the already-retrieved excerpts (bounded `context` window) via
   the same sibling-window logic `get_section(..., context=N)` uses (kb/mcp_server.py),
   and re-call **once** — same one-shot, gated-re-query shape as `step: rewrite` above,
   not an open-ended agentic loop. `validate_and_enrich`'s guarantee is unchanged by this:
   it still only proves a citation resolves, never that its text was read with enough
   context to support the claim beside it.
6. `step: validate` — `validate_and_enrich(payload, strict=false)` (lenient: a
   hallucinated citation becomes a warning shown in the UI, not a failed response — a
   user should still see the prose even if one citation didn't resolve).
7. Persist: insert into `answers` (`kind=chat_message`), insert a `messages` row
   (`role=assistant`, `answer_id=...`), stream the terminal `done` event with the full
   enriched payload.

## UI additions (on top of the navigator's SPA)

- **Conversation sidebar** (decision: multiple named threads) — list, new/rename/delete,
  click to load `GET /api/conversations/{id}/messages` and replay them into the chat
  panel (each assistant message re-rendered via the same chip-injection code the
  document viewer uses — one rendering path for "prose with citations," reused for chat
  bubbles, wiki pages, and standalone answers alike).
- **Chat panel** — question input, streaming step indicator (spherex's per-step
  stopwatch pattern), rendered answer with clickable `[n]` chips.
- **Answers sidebar** — list of stored answers (`GET /api/answers`), filterable by
  `kind`; clicking one navigates to `#answer=<id>` and renders it in the main pane
  (this is how a `publish_answer`-created `agent_adhoc` answer gets seen — no push, just
  a normal list refresh/page load).
- **Search box** — hits FTS5 `GET /api/search`, results link straight to
  `#doc=<doc_id>&node=<node_id>` (reusing the same anchor/scroll mechanism citations
  use — search results and citations resolve through one code path).
- **Citation click-through**: unchanged mechanics from the navigator — chip injection
  via `TreeWalker` (skip `CODE`/`PRE`/`A`/`BUTTON` so literal `[1]` in code/links
  survives), click → `setActiveDoc` → `scrollAndFlash(node_id)` → now resolving to a
  paragraph/table/figure anchor instead of a whole section, so the flash highlight lands
  tightly on the exact cited content.

## Ops

```bash
uv venv webapp/.venv --python 3.12
uv pip install -p webapp/.venv/bin/python -r webapp/requirements.txt   # historical: this
    # file was deleted in the papertrail migration's Step 11 — use `uv pip install -e
    # .[web]` against pyproject.toml instead, the real dependency list now
BASE_URL=... API_KEY=... CHAT_MODEL=anthropic/claude-sonnet-5 \
  webapp/.venv/bin/python -m uvicorn webapp.app.main:app --reload
```
Env: `BASE_URL`, `API_KEY`, `CHAT_MODEL` (read at chat-call time, same
`OpenAI(base_url=..., api_key=...)` client construction pattern as
`kasi_papers_analysis/kb/config.py`); `KB_SQLITE`/`KB_DIR` overridable for pointing at a
different corpus per `05_adoption.md`. No auth, no CORS hardening, bind `127.0.0.1` only
(kasi's docker-compose comment: "the server has no auth… do NOT bind 0.0.0.0 here" —
same warning applies here even without Docker).

*(Migration note: `papertrail`'s equivalent is `--corpus PATH` or `PAPERTRAIL_CORPUS`,
serving via `.venv/bin/papertrail serve --corpus corpora/wasp12b` /
`uvicorn papertrail.web.asgi:app`, with `KB_DIR`/`KB_SQLITE` still honored for one
release as a compatibility alias — see `05_adoption.md` step 9. Not yet the live command.)*

Static export (P4, `00_overview.md` roadmap): `build_static.py` ported from the
navigator, extended to also bake a chosen conversation's full message history
(`--conversation <id>` in addition to the existing `--result payload.json`) into an
offline single-file artifact — useful for sharing a specific investigation without
running the server.
