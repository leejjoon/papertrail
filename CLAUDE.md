# wasp12b_papers

A knowledge base over ~210 papers mentioning the exoplanet WASP-12b: acquisition →
extraction/classification/wiki pipeline → SQLite FTS index → a web app with chat → an
MCP server for coding agents. Full design rationale lives in `docs/spec/` — read that
before making structural changes; this file is operational orientation, not a spec.

## Repo layout

```
wasp_12b_papers.yaml        source corpus (210 entries, from ADS bibcode search) — gitignored
paper_download/             Stage 0 — PDF acquisition (done, not touched again unless
                             the corpus grows). See paper_download/PAPER_DOWNLOAD_HOWTO.md
                             and paper_download/data/FAILED_PDFS.md. Scripts are tracked;
                             paper_download/data/ (PDFs, Marker JSON) is gitignored.
kb/                         the pipeline + MCP server (code tracked; everything below
                             that's generated from paper content is gitignored)
  corpus.yaml                taxonomy, wiki heading contract, model env-var names
  config.py                   env vars, paths — mirrors paper_download/config.py
  adapters/wasp12b.py         metadata adapter (wraps paper_download/config.load_papers())
  build_sections.py           Stage A: Marker JSON -> section-tree JSON + image assets
  classify.py                 Stage B: LLM taxonomy tagging (BULK_MODEL)
  paper_pages.py               Stage C: cited wiki paper pages (BULK_MODEL)
  topic_pages.py               Stage D: cross-paper cited topic pages + index (SYNTH_MODEL)
  build_index.py               Stage E: builds kb/data/kb.sqlite
  mcp_server.py                MCP server, 9 tools (see docs/spec/04_mcp.md)
  data/                        sections/, assets/, classification/, kb.sqlite — gitignored
  wiki/                        papers/*.md, topics/*.md, index.md — LLM prose derived from
                               paper content, gitignored (each pipeline run regenerates it
                               from kb/data/ + the corpus; not the durable artifact)
webapp/                      FastAPI + SPA — viewer, search, chat, answer store
  app/{config,registry,renderer,answer,store,search,chat,main}.py
  static/index.html            single-file SPA (vanilla JS, no build step)
docs/spec/                   00_overview -> 05_adoption — the actual design doc set
.claude/skills/wasp12b-answer/  project skill: search -> read -> cite -> validate -> publish
.mcp.json                    registers kb/mcp_server.py as "wasp12b-kb"
answers/                     ad-hoc markdown exports of published answers (not auto-managed
                             — someone writes here by hand/agent when they want a
                             shareable copy outside kb.sqlite; not required workflow) —
                             gitignored, same reasoning as kb/wiki/
```

Git tracks code and design docs only (`kb/*.py`, `webapp/`, `paper_download/*.py`,
`docs/spec/`, this file, `.mcp.json`, the skill) — no papers and nothing derived from
paper content. Anyone cloning the repo needs to supply `wasp_12b_papers.yaml` and rerun
the pipeline (`paper_download/` → `kb/build_sections.py` → ... → `kb/build_index.py`) to
regenerate `kb/data/`, `kb/wiki/`, and `kb/data/kb.sqlite` locally.

## Running things

```bash
# pipeline (idempotent, --dry-run/--force/--only DOC_ID on every stage)
.venv/bin/python kb/build_sections.py
.venv/bin/python kb/classify.py
.venv/bin/python kb/paper_pages.py
.venv/bin/python kb/topic_pages.py
.venv/bin/python kb/build_index.py

# web app (no fixed port convention — 8009 is just what's running now; check with
# `ps aux | grep uvicorn` before assuming a port, ports 8000/8010 on this machine
# are OTHER unrelated projects/services, not this one)
.venv/bin/python -m uvicorn webapp.app.main:app --port 8009 --host 0.0.0.0

# MCP server (stdio; also registered as "wasp12b-kb" in .mcp.json)
.venv/bin/python kb/mcp_server.py
```

`.env` holds `ADS_API_TOKEN`, `DATALAB_API_KEY`, `BASE_URL`/`API_KEY` (LLM gateway),
`BULK_MODEL` (deepseek/deepseek-v4-flash-0731), `SYNTH_MODEL` (anthropic/claude-opus-4.8),
`CHAT_MODEL` (anthropic/claude-sonnet-5) — copied from `~/projects/kasi_papers_analysis/.env`
originally; this project's own credentials now live here, not there.

## The one thing to know before touching citations

Every producer of cited content (chat, `paper_pages.py`, `topic_pages.py`, MCP's
`publish_answer`) shares one contract: `[n]` markers + a `citations[]` list resolving to
real `(doc_id, node_id)` pairs, validated by `webapp/app/answer.py`'s
`validate_and_enrich()`. That function checks two different things — don't confuse them:

1. **Every `[n]` used resolves to a real citation** (hard check, raises in strict mode).
2. **Every author-year mention (`"Madhusudhan et al. (2011)"`) has an `[n]` nearby**
   (`find_uncited_author_year_mentions`, non-fatal `warnings` only). This exists because
   (1) alone let a real bug through: an LLM can name a study by author-year and just
   never attach `[n]`, and the "does every marker resolve" check has nothing to catch —
   there's no marker to check. This heuristic has a real false-positive rate on long,
   correctly-cited sentences (verified: legitimate citations placed 100-345 chars after
   their author-year mention) — it's a signal to review, not a hard gate everywhere.
   `paper_pages.py`/`topic_pages.py` treat it as report-only (a batch job retrying on
   every heuristic false positive wastes real cost); chat.py treats it as a bounded
   retry-once trigger, since interactive latency for one extra call is cheap.

**Known gap, left as a deliberate decision, not an oversight:** the existing 190 wiki
pages under `kb/wiki/papers/` were generated *before* this heuristic existed — a 40-page
sample found 24/50 author-year mentions with no citation at all. Chat and any future
pipeline runs are fixed; the existing corpus was not regenerated (real cost tradeoff,
user's call). Don't assume every claim in an existing wiki page is cited just because it
reads fluently — spot-check before treating one as ground truth for something important.

## Skills

`/wasp12b-answer` — drives the MCP server's search → read → cite → validate → publish
loop correctly, including the warnings-not-just-errors distinction above. Use this
(or read it before hand-rolling the same sequence) whenever asked to answer a
question from this corpus with proper citations.
