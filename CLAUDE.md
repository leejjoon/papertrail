# wasp12b_papers

A knowledge base over ~210 papers mentioning the exoplanet WASP-12b: acquisition →
extraction/classification/wiki pipeline → SQLite FTS index → a web app with chat → an
MCP server for coding agents. Full design rationale lives in `docs/spec/` — read that
before making structural changes; this file is operational orientation, not a spec.

## ⚠️ Mid-migration: the pipeline is being extracted into a generic package

This repo's `kb/`/`webapp/` code (WASP-12b-specific, hardcoded prompts/facets/branding) is
being extracted into a reusable, corpus-agnostic package, `papertrail`
(`src/papertrail/`), with wasp12b becoming its first consumer corpus
(`corpora/wasp12b/`). Full plan: `.claude/plans/i-would-like-to-stateless-hellman.md`.

**As of this writing: Steps 0–10 of that plan are done, verified against a
`corpora/wasp12b/` copy, and committed — but NOT yet cut over.** `kb/` below is still the
ground truth for the real corpus's `kb/data/`/`kb/wiki/`. Don't delete it, and don't
point the live processes at `corpora/wasp12b/` or `src/papertrail/`'s schema without an
explicit, deliberate cutover (restarting both services, reconnecting the MCP server) —
that's Step 12 territory, done once the remaining migration steps (a demo-corpus proof)
are complete. Until then, treat `src/papertrail/` + `corpora/wasp12b/` as
verified-but-inert: real code, real tests, real LLM round-trips against a copy of the
data, but not yet the thing that's supposed to be serving requests.

**Important nuance, don't skip this:** "not yet cut over" describes intent, not the
current on-disk reality — the code moves already happened underneath the still-running
processes. `webapp/app/main.py` (imported by the running `uvicorn webapp.app.main:app`)
is now a 2-line shim that imports `papertrail.web.asgi:app`; `webapp/app/registry.py` and
`answer.py` were deleted (Step 9) once nothing outside `papertrail` still imported them.
`kb/mcp_server.py` was deleted (Step 5); `.mcp.json` already points at
`.venv/bin/papertrail mcp` `PAPERTRAIL_CORPUS=kb`. **But the two long-running processes
themselves have Python module state cached from before these changes** — a webapp
process only re-imports code on restart, and an MCP server process Claude Code is
already connected to keeps running whatever it loaded at connect time even after its
source file is deleted from disk. Concretely: the webapp process (`ps aux | grep
uvicorn`) has NOT been restarted since early in the migration, so it's running
Step-4-era `papertrail` code against the real `kb.sqlite`'s OLD schema — safe only
because Step-4-era code doesn't know about `doc_facets`/`facets_json` yet. **All code
committed since Step 7 (schema, identity, taxonomy) is schema-incompatible with the
real, unmigrated `kb/data/kb.sqlite`.** The registered MCP server process is similarly
running the old, now-deleted `kb/mcp_server.py` entirely from memory — it will keep
answering tool calls until Claude Code reconnects the session, at which point it will
re-launch via the new `.mcp.json` command and immediately hit the same schema mismatch.
**Do not restart the webapp process or trigger an MCP reconnect** without deliberately
handling this — that's exactly the cutover this migration is staging for, not an
incidental side effect to trigger early.

```
src/papertrail/             the extracted, corpus-agnostic package (not yet live)
  config.py                  Settings/Corpus — corpus.yaml resolution, env overrides
  identity.py                 CitationStyle (label/reference_label/external_links/
                              canonicalize/uncited-mention heuristic) — pluggable per corpus
  taxonomy.py                  FacetSpec/Navigation — taxonomy + doc-tree/table config
  prompts.py + prompts/*.txt    LLM system-prompt templates, corpus.yaml-interpolated
  textutils.py                slugify
  core/                       FastAPI-free: registry, renderer, citations (validate_and_
                              enrich), store, search, schema (sqlite DDL), nodes (tree walkers)
  pipeline/                   Stages A-E: sections, classify, paper_pages, topic_pages, index
  web/                         FastAPI app factory + chat + the packaged static/index.html
  mcp/server.py                MCP server, build_server(settings) factory
  cli.py                       `papertrail <stage>` console script
corpora/wasp12b/             wasp12b as a self-contained papertrail corpus (a COPY of
                              kb/data + kb/wiki for now, not yet the live copy)
  corpus.yaml                 taxonomy, identity, navigation, prompt subject/citation_example
  adapter.py                   metadata adapter (wraps paper_download/config.load_papers())
  style.py                     Wasp12bStyle — bibcode '&'/'+' canonicalization
  data/, wiki/                 gitignored, mirrors kb/data + kb/wiki
tests/                       characterization tests against papertrail (none existed
                              pre-migration) — pytest, `.venv/bin/python -m pytest tests/`
```

## Repo layout (the LIVE code — kb/ + webapp/)

```
wasp_12b_papers.yaml        source corpus (210 entries, from ADS bibcode search) — gitignored
paper_download/             Stage 0 — PDF acquisition (done, not touched again unless
                             the corpus grows). See paper_download/PAPER_DOWNLOAD_HOWTO.md
                             and paper_download/data/FAILED_PDFS.md. Scripts are tracked;
                             paper_download/data/ (PDFs, Marker JSON) is gitignored.
kb/                         the LIVE pipeline's data + corpus config (code mostly gone —
                             see below; everything generated from paper content is
                             gitignored)
  corpus.yaml                taxonomy, wiki heading contract, model env-var names — this
                              is what corpora/wasp12b/corpus.yaml above was copied from
  config.py                   still here (unmigrated pre-Step-4 module-global config) but
                               nothing currently imports it — dead code, not yet deleted
  adapters/wasp12b.py         still here for the same reason (superseded by
                               corpora/wasp12b/adapter.py); not yet deleted
  data/                        sections/, assets/, classification/, kb.sqlite — gitignored,
                               THE real corpus data, old schema (no doc_facets/facets_json)
  wiki/                        papers/*.md, topics/*.md, index.md — the real, durable wiki
                               content; NOT regenerated by any pipeline run since Step 8
                               moved the pipeline scripts themselves into src/papertrail/
  (build_sections.py / classify.py / paper_pages.py / topic_pages.py / build_index.py /
   mcp_server.py — all DELETED, moved to src/papertrail/{pipeline,mcp}/ in Steps 5-6.
   kb/ is now a corpus directory in fact, just not the one anything currently reads from.)
webapp/                      FastAPI app — now nearly empty. webapp/app/main.py is a 2-line
                             shim importing papertrail.web.asgi:app (so the run command
                             below keeps working); webapp/app/registry.py and answer.py
                             were DELETED in Step 9 once src/papertrail/core/{registry,
                             citations}.py fully replaced them. webapp/requirements.txt was
                             deleted in Step 11 (superseded by pyproject.toml's [web] extra).
docs/spec/                   00_overview -> 05_adoption — the actual design doc set. Written
                             for the pre-migration kb/+webapp/ layout; 05_adoption.md in
                             particular documented some genericity claims that turned out
                             to be false against the actual code — that's the whole reason
                             this migration exists. Each doc now carries a "migration note"
                             callout pointing at its src/papertrail/ equivalent.
.claude/skills/wasp12b-answer/  project skill: search -> read -> cite -> validate -> publish
.mcp.json                    ALREADY updated (Step 5) to register `.venv/bin/papertrail mcp`
                             with `PAPERTRAIL_CORPUS=kb` — not "kb/mcp_server.py" (deleted).
                             The currently-connected MCP session, however, is still running
                             the OLD, now-deleted kb/mcp_server.py entirely from memory (see
                             the nuance callout above) until Claude Code reconnects it.
answers/                     ad-hoc markdown exports of published answers (not auto-managed
                             — someone writes here by hand/agent when they want a
                             shareable copy outside kb.sqlite; not required workflow) —
                             gitignored, same reasoning as kb/wiki/
```

Git tracks code and design docs only (`kb/*.py`, `webapp/`, `paper_download/*.py`,
`src/papertrail/`, `corpora/wasp12b/{corpus.yaml,adapter.py,style.py}`, `docs/spec/`, this
file, `.mcp.json`, the skill, `tests/`, `pyproject.toml`) — no papers and nothing derived
from paper content. Anyone cloning the repo needs to supply `wasp_12b_papers.yaml` and
rerun the pipeline to regenerate `kb/data/`/`corpora/wasp12b/data/`, `kb/wiki/`/
`corpora/wasp12b/wiki/`, and the sqlite file locally.

## Running things

The pipeline scripts under `kb/` no longer exist (moved to `src/papertrail/pipeline/` in
Step 6) — there is no "run the live pipeline against `kb/`" command anymore in the old
`kb/*.py` form. **`kb/corpus.yaml` itself was deliberately never updated with the new
seam fields** (`navigation:`, per-facet `label`/`slug_prefix`/`topic_pages`/
`similarity_weight`, `identity:`, `corpus.title`/`subject`/`citation_example` — only
`corpora/wasp12b/corpus.yaml` has these, added across Step 9). So `.venv/bin/papertrail
<stage> --corpus kb` **will fail eager validation right now**, not run against stale
data quietly — Seams 2-4's config loaders (`taxonomy.py`, `prompts.py`) require fields
`kb/corpus.yaml` doesn't have. There is currently no working command to regenerate
`kb/data/`/`kb/wiki/` with the new pipeline code without first either updating
`kb/corpus.yaml` to the new shape or replacing `kb/`'s data with `corpora/wasp12b/`'s —
both are real cutover decisions, not yet made (see the migration banner above).

```bash
# THE LIVE WEB APP PROCESS (no fixed port convention — 8009 is just what's running now;
# check with `ps aux | grep uvicorn` before assuming a port, ports 8000/8010 on this
# machine are OTHER unrelated projects/services, not this one). webapp.app.main:app is a
# shim to papertrail.web.asgi:app, but the RUNNING process hasn't picked up recent
# src/papertrail/ changes since it hasn't restarted — see the nuance callout above before
# touching this:
.venv/bin/python -m uvicorn webapp.app.main:app --port 8009 --host 0.0.0.0
# equivalent, once actually cutting over: PAPERTRAIL_CORPUS=kb .venv/bin/python -m uvicorn papertrail.web.asgi:app --port 8009

# THE MCP SERVER — .mcp.json already registers `.venv/bin/papertrail mcp` with
# PAPERTRAIL_CORPUS=kb (Step 5), but the CONNECTED session process is still the old,
# deleted kb/mcp_server.py running from memory — again, see the nuance callout above.
# Manually invoking today's real entry point looks like:
PAPERTRAIL_CORPUS=kb .venv/bin/papertrail mcp

# --- papertrail against the VERIFIED-BUT-INERT corpora/wasp12b/ copy (safe to run
# freely — this is a copy, not the real kb/data/, and is what all Step 0-10 verification
# actually ran against) ---
pip install -e .[web,mcp]                                  # once, into .venv
.venv/bin/papertrail sections    --corpus corpora/wasp12b
.venv/bin/papertrail classify    --corpus corpora/wasp12b
.venv/bin/papertrail paper-pages --corpus corpora/wasp12b
.venv/bin/papertrail topic-pages --corpus corpora/wasp12b
.venv/bin/papertrail index       --corpus corpora/wasp12b
PAPERTRAIL_CORPUS=corpora/wasp12b .venv/bin/python -m uvicorn papertrail.web.asgi:app --port 8011
PAPERTRAIL_CORPUS=corpora/wasp12b .venv/bin/papertrail mcp
.venv/bin/python -m pytest tests/                            # characterization test suite
```

`.env` holds `ADS_API_TOKEN`, `DATALAB_API_KEY`, `BASE_URL`/`API_KEY` (LLM gateway),
`BULK_MODEL` (deepseek/deepseek-v4-flash-0731), `SYNTH_MODEL` (anthropic/claude-opus-4.8),
`CHAT_MODEL` (anthropic/claude-sonnet-5) — copied from `~/projects/kasi_papers_analysis/.env`
originally; this project's own credentials now live here, not there.

## The one thing to know before touching citations

Every producer of cited content (chat, Stage C paper pages, Stage D topic pages, MCP's
`publish_answer`) shares one contract: `[n]` markers + a `citations[]` list resolving to
real `(doc_id, node_id)` pairs, validated by `validate_and_enrich()` — now solely
`src/papertrail/core/citations.py` (`webapp/app/answer.py` was deleted in Step 9 once
this was its only remaining consumer; the currently-running webapp process still has the
old copy loaded in memory from before that deletion, per the nuance callout above). That
function checks two different things — don't confuse them:

1. **Every `[n]` used resolves to a real citation** (hard check, raises in strict mode).
2. **Every author-year mention (`"Madhusudhan et al. (2011)"`) has an `[n]` nearby**
   (`find_uncited_author_year_mentions`, non-fatal `warnings` only). This exists because
   (1) alone let a real bug through: an LLM can name a study by author-year and just
   never attach `[n]`, and the "does every marker resolve" check has nothing to catch —
   there's no marker to check. This heuristic has a real false-positive rate on long,
   correctly-cited sentences (verified: legitimate citations placed 100-345 chars after
   their author-year mention) — it's a signal to review, not a hard gate everywhere.
   Stage C/D treat it as report-only (a batch job retrying on every heuristic false
   positive wastes real cost); chat treats it as a bounded retry-once trigger, since
   interactive latency for one extra call is cheap. In the migration, this heuristic
   lives behind a pluggable `CitationStyle` (`src/papertrail/identity.py`) — a corpus
   without an author-year convention gets it disabled entirely (`PlainStyle`), rather
   than false-positiving on prose it was never calibrated against.

**Known gap, left as a deliberate decision, not an oversight:** the existing 190 wiki
pages under `kb/wiki/papers/` were generated *before* this heuristic existed — a 40-page
sample found 24/50 author-year mentions with no citation at all. Chat and any future
pipeline runs are fixed; the existing corpus was not regenerated (real cost tradeoff,
user's call). Don't assume every claim in an existing wiki page is cited just because it
reads fluently — spot-check before treating one as ground truth for something important.

**A resolving `[n]` also doesn't mean the node was read in enough context.** Retrieval is
paragraph-granular, and a single paragraph read alone is often ambiguous — an unresolved
pronoun/comparison ("this discrepancy," "the equilibrium case"), or a paper-specific term
undefined in that node (e.g. what a paper's "C/O ratio" assumes). `validate_and_enrich()`
only proves a citation resolves to a real node, never that its text supports the claim
next to it. Two escalation paths exist: `get_section(doc_id, node_id, context=N)` widens
with sibling nodes (agent tool-calling — `src/papertrail/mcp/server.py` now, the only
copy that exists on disk; the connected MCP session is still running the deleted
`kb/mcp_server.py`'s version from memory, per the nuance callout above); chat's
`need_context` field does the same for the interactive path, bounded to one round. Both
share `context_window()`'s sibling-window semantics, now `src/papertrail/core/nodes.py`
(`webapp/app/registry.py`, the old home of this logic, was deleted in Step 9).

## Skills

`/wasp12b-answer` — drives the MCP server's search → read → cite → validate → publish
loop correctly, including the warnings-not-just-errors distinction above. Use this
(or read it before hand-rolling the same sequence) whenever asked to answer a
question from this corpus with proper citations.
