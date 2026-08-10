# WASP-12b KB — Overview

## Status

**Stale header, corrected:** the paragraph below ("this is a specification, not yet an
implementation... nothing under `kb/` or `webapp/` exists yet") was accurate when this
doc was first written and was never updated once P1–P4 actually shipped — kept below
verbatim as a historical note, not current fact. The real current status, as of the
`papertrail` extraction (see `CLAUDE.md`'s "⚠️ Mid-migration" section and
`05_adoption.md`): `kb/` + `webapp/` are the **live**, working implementation of
everything this document set describes, and have been for most of this project's life.
In parallel, that same functionality has been extracted into a reusable, corpus-agnostic
package, `src/papertrail/` (WASP-12b becoming its first consumer corpus,
`corpora/wasp12b/`) — verified equivalent to `kb/`/`webapp/` but not yet the live system.
`CLAUDE.md` is the up-to-date map of what's live vs. in-migration; this spec set
describes the *design*, most of which the migration preserved rather than changed (see
`05_adoption.md` for exactly what genericity work the migration added on top of it).

*(Original text, historical:)* This is a **specification**, not yet an implementation. It
defines the system to build on top of the 190 papers already downloaded and
Marker-converted (`paper_download/data/marker/*.json`, see
`paper_download/PAPER_DOWNLOAD_HOWTO.md` and `paper_download/data/FAILED_PDFS.md` for how
that corpus was assembled). Nothing under `kb/` or `webapp/` exists yet — this document
set is the plan those directories will implement.

## Vision

A knowledge base over ~210 papers mentioning WASP-12b (190 with full text, 20 with
metadata only — see [Corpus completeness](#corpus-completeness)), queryable two ways:

1. **A web app with chat.** Ask a question ("what orbital-decay rate do these papers
   converge on?"); the backend searches the corpus, calls an LLM, and shows an answer
   with inline citations. Clicking a citation jumps to the exact paragraph, table, or
   figure it came from.
2. **An MCP server for coding agents.** Claude Code (or any MCP client) gets the same
   search/read tools, plus the ability to draft an answer, validate that every citation
   actually resolves, and **publish** it — at which point it appears in the web app with
   the identical citation UX a human's chat answer would get.

## The unifying principle

Everything the system produces that makes a claim and backs it with sources is the
**same artifact**: an *answer payload* — markdown prose with inline `[n]` markers, plus
a `citations[]` list mapping each `n` to a `(doc_id, node_id)` pair identifying the exact
paragraph/table/figure/equation it supports. This is not a chat-specific format; it is
the *only* format anything in this system uses to make a citable claim:

| Producer | When generated | `kind` | Scope of citations |
|---|---|---|---|
| Chat | interactively, per question | `chat_message` | usually one paper, sometimes several |
| Paper wiki page | in bulk, pipeline Stage C | `wiki_paper` | that paper's own nodes |
| Topic wiki page | in bulk, pipeline Stage D | `wiki_topic` | nodes across the topic's member papers |
| Agent one-off | on demand, via MCP `publish_answer` | `agent_adhoc` | whatever the agent read |

All four are validated by the same checker (`validate_and_enrich`, extended from
`multi_doc_navigator`'s `app/answer.py`) and stored as rows in one `answers` table (see
`01_data_contracts.md`). A wiki page is not a separate content type that happens to look
similar to a chat answer — it *is* an answer payload, just authored once per paper
instead of once per question, by the pipeline instead of by a live LLM call sitting in
the chat request path. This is why the pipeline's paper/topic-page generation stages
(02_pipeline.md, Stages C–D) and the web app's chat endpoint (03_webapp.md) both bottom
out in the same validation and storage code.

The practical payoff: build the citation-rendering machinery (chip injection,
scroll-to-node, source panel) once, in the web app, and every producer — human asking a
question, the nightly pipeline run, or an agent — gets it for free.

## Two reference implementations, and what's carried from each

This spec was designed by reading two existing, working systems in depth rather than
inventing from scratch:

- **`~/projects/kasi_papers_analysis/kb/`** supplies the *pipeline* half: Marker JSON →
  extracted text → LLM-generated wiki pages → topic pages → SQLite FTS5 index → an MCP
  server with 6 read-only tools. Patterns carried over: heading-contract-as-schema
  (generate, check required headings are present, retry on failure — no JSON mode
  needed), a hard deterministic/LLM split (anything that can go stale — links, counts,
  author roles — is templated from data; only prose is LLM-written), hierarchical
  summarization (cheap model summarizes each paper, expensive model reads only those
  summaries to write a topic page), provenance frontmatter
  (`generator: {model, prompt_version, generated_at}`), progressive-disclosure MCP tools
  (a tool called with no arguments teaches the caller what arguments are valid), the
  `fts_escape`/`bm25()`/`snippet()` FTS5 query conventions, and opening SQLite read-only
  via a `file:...?mode=ro` URI so the MCP server can never corrupt the index.
- **`~/projects/multi_doc_navigator/`** supplies the *presentation* half: a
  section-tree JSON per document with stable `node_id` anchors, the answer-payload
  contract described above (`docs/payload.schema.json` in that repo),
  `validate_and_enrich()` as one FastAPI-free function shared by the server, a CLI
  validator, and a static-site exporter, chip-based citation rendering (post-process the
  rendered HTML with a `TreeWalker`, replacing `[n]` text nodes with clickable buttons —
  skipping anything inside `CODE`/`PRE`/`A`/`BUTTON` so literal brackets in code or links
  survive), and an agent-authoring skill (`corpus.py` search/read tools +
  `validate_payload.py --resolve` in a loop until valid) that is the direct ancestor of
  this spec's MCP authoring workflow (`04_mcp.md`).

Neither is used unmodified — see `02_pipeline.md` for the specific fixes carried over
from kasi (notably the ListGroup/bibliography bug) and `01_data_contracts.md` for how the
node model was pushed to finer granularity than either reference implementation used.

## Architecture

```
wasp_12b_papers.yaml + paper_download/data/{ads_meta,pdf_manifest}.json   (existing)
                    │
paper_download/data/marker/*.json  (existing, 190 files — already carry per-block
                    │                image data despite disable_image_extraction=true;
                    │                see 02_pipeline.md Stage A)
                    ▼
   kb/corpus.yaml  ──────────────────────────────────────────────┐
                    │                                             │ (metadata adapter,
     Stage A  build_sections.py    → kb/data/sections/*.json      │  taxonomy, heading
                                    → kb/data/assets/<doc_id>/*    │  contract — all
     Stage B  classify.py          → kb/data/classification/*.yaml│  corpus-specific,
     Stage C  paper_pages.py       → kb/wiki/papers/*.md          │  see corpus.yaml
     Stage D  topic_pages.py       → kb/wiki/topics/*.md          │  in
     Stage E  build_index.py       → kb/data/kb.sqlite            │  01_data_contracts.md)
                    │                                             │
                    ▼                                             │
        ┌───────────┴────────────┐                                │
        ▼                        ▼                                │
   webapp/ (FastAPI+SPA)    kb/mcp_server.py  ◄──────── coding agents (Claude Code, …)
   chat · search · viewer   9 tools: search/read + validate_answer/publish_answer
        │                        │
        └──────────┬─────────────┘
                    ▼
        kb.sqlite `answers` table (chat_message | wiki_paper | wiki_topic | agent_adhoc)
```

Full schemas for every artifact above are in `01_data_contracts.md`; the five pipeline
stages (plus the Stage 0 rework) are detailed in `02_pipeline.md`; the web app in
`03_webapp.md`; the MCP server in `04_mcp.md`; and how to point this whole toolchain at
a *different* Marker corpus in `05_adoption.md`.

## Corpus completeness

`wasp_12b_papers.yaml` lists 210 papers. Not all 210 get full treatment:

- **190** have a downloaded, Marker-converted PDF (`paper_download/data/pdf_manifest.json`
  status `ok`) — these get the full pipeline: sections, classification, wiki page,
  indexing.
- **20** do not: 2 papers whose PDF was never obtained (Astrobiology, JATIS —
  genuinely paywalled/bot-blocked, see `paper_download/data/FAILED_PDFS.md`) plus 18
  skipped by the acquisition pipeline as not-a-journal-article (meeting abstracts, book
  chapters, arXiv-only entries, entries with no bibcode). These get a **metadata-only
  stub** in the KB (title, authors, year, journal — the source YAML carries no abstract
  field, so there is no text to index beyond that) rather than being silently absent, so
  a question like "is there a 2009 conference abstract about WASP-12b's discovery?"
  doesn't read as "the KB has never heard of this," when the real answer is "it exists,
  but there's no full text to search or cite." See `01_data_contracts.md` for the exact
  `docs` row shape.
- One paper, `2010RPPh...73a6901B`, has real full text but **not from the publisher** —
  its PDF is an arXiv preprint substituted by hand after the publisher version proved
  unobtainable (see `FAILED_PDFS.md`). It is not one of the 20 stubs; it gets full
  pipeline treatment, just carrying a `fulltext_source: arxiv_preprint` flag (vs.
  `publisher_pdf` for the other 189) so an answer can be honest about provenance if it
  ever matters.

## Non-goals (v1)

- **No embeddings / vector search.** FTS5 BM25 over paragraph-granular nodes is the
  full retrieval story (see `01_data_contracts.md` node granularity and
  `03_webapp.md` chat retrieval flow). Adding a vector column later is an isolated,
  additive change — not a prerequisite for anything in this spec.
- **No auth, no multi-user state, no CORS hardening.** Single-user, localhost, matching
  both reference implementations' assumptions.
- **No live push (SSE/WebSocket) between an agent publishing an answer and an
  already-open browser tab.** `publish_answer` returns a stable `{id, url}`; the web app
  resolves it on page load. See `03_webapp.md`.
- **No span-level highlighting.** Citation granularity is the node (a paragraph, one
  table, one figure, one equation, one bibliography entry) — matching the navigator's
  `quote` field, which is reserved but unused.

## Repo layout

**This shows the original `kb/`/`webapp/` layout only** — still the live layout today.
For the parallel, generalized `src/papertrail/` + `corpora/wasp12b/` layout the
migration produced (not yet live), see `CLAUDE.md`.

```
kb/
  config.py              # env vars, paths — mirrors paper_download/config.py
  corpus.yaml             # this corpus's taxonomy/heading-contract/adapter config
  build_sections.py       # Stage A
  classify.py             # Stage B
  paper_pages.py          # Stage C
  topic_pages.py          # Stage D
  build_index.py          # Stage E
  mcp_server.py
  data/
    sections/<doc_id>.json
    assets/<doc_id>/<block_id>.jpeg
    classification/<doc_id>.yaml
    kb.sqlite
  wiki/
    papers/<doc_id>.md
    topics/*.md
    index.md
webapp/
  app/
    config.py registry.py renderer.py answer.py chat.py search.py
  main.py
  static/index.html
docs/spec/               # this document set
paper_download/          # unchanged — acquisition only, out of scope here
```

## Phased roadmap

**P1–P4 below are all complete** (this is the live `kb/`/`webapp/` system described
throughout this doc set). An unlisted **P5**, the `papertrail` extraction, is in progress
on top of it — see `CLAUDE.md` and `05_adoption.md`.

1. **P1 — Pipeline + index.** Stage A through Stage E over the existing 190 Marker
   JSONs (no reconversion needed — see `02_pipeline.md` Stage A). Produces `kb.sqlite`,
   wiki pages, section trees, assets. No web app or MCP yet — verifiable by direct SQL
   and by eyeballing wiki pages.
2. **P2 — MCP server.** Read tools first (`search_fulltext` … `get_figure_image`), then
   the authoring tools (`validate_answer`, `publish_answer`) once the `answers` table
   exists. Usable by Claude Code immediately after P1, ahead of the web app existing.
3. **P3 — Web app + chat.** Viewer (copied/adapted navigator seam) first — confirms
   `kb.sqlite`/section-tree data renders correctly before adding chat on top. Then
   `/api/chat`, conversation threads, the answers sidebar.
4. **P4 — Polish.** Static export (navigator's `build_static.py` pattern, for sharing a
   read-only snapshot), optional Docker packaging (kasi's ro-mount pattern), and the
   explicit future option of adding embeddings if FTS5 recall proves insufficient in
   practice.
