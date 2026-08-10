# Quickstart

Fifteen minutes, one small corpus, every command run for real. This walks through
`corpora/demo/` — a throwaway 5-paper corpus deliberately configured to look nothing like
this repo's main corpus (different subject, different taxonomy, different citation
style) — which is itself the proof that none of this is hardcoded to any one corpus's
shape. Every command block below and its output was run against this exact corpus while
writing this doc.

For adopting `papertrail` for your own documents instead of following along with the
demo, see [USERS_GUIDE.md](USERS_GUIDE.md). For every flag/field/endpoint this quickstart
glosses over, see [REFERENCE.md](REFERENCE.md).

## Prerequisites

- Python ≥3.11.
- An OpenAI-compatible LLM gateway: a `BASE_URL` and `API_KEY` in `.env`. This
  repo's own `.env` also sets `BULK_MODEL`/`SYNTH_MODEL`/`CHAT_MODEL` — the package
  defaults for these three are literal model identifiers for one specific gateway and
  will 404 against a generic OpenAI endpoint, so set all three explicitly for yours.
- A directory of Marker JSON (Datalab-hosted-API output) already on disk. Acquiring
  that is out of scope for papertrail itself — this repo's
  `paper_download/PAPER_DOWNLOAD_HOWTO.md` documents one way; `corpora/demo/` reuses 5
  of this repo's own already-converted files as fixture data.

## 1. Install

```bash
pip install -e '.[web,mcp]'
```

## 2. Look at the corpus you're about to build

`corpora/demo/corpus.yaml`'s `corpus:` block — the entire difference between this corpus
and this repo's main one is config, not code:

```yaml
corpus:
  name: demo
  title: Demo Knowledge Base
  description: A throwaway corpus proving the papertrail pipeline needs no
    WASP-12b-specific code — see docs/spec/05_adoption.md's Step 12.
  glyph: "🧪"
  subject: hot Jupiter exoplanets
  citation_example:
    bad: "Fortney et al. (2007) modeled the atmospheric structure, in contrast to earlier isothermal treatments."
    good: "Fortney et al. (2007) modeled the atmospheric structure [3], in contrast to earlier isothermal treatments [3]."
```

Further down, its taxonomy has 2 facets (`method`, `outcome`) where this repo's main
corpus has 4; its `wiki_headings:` has 3 entries where the other has 6; its
`identity.citation_style` is `plain` (no author-year assumptions, no external links) —
see `corpora/demo/corpus.yaml` in full, and [USERS_GUIDE.md §4](USERS_GUIDE.md#4-writing-corpusyaml)
for what each choice means.

## 3. Run the pipeline

```bash
papertrail sections     --corpus corpora/demo   # Stage A
```
```
Papers: 5 | already built: 0 | no marker JSON: 0 | to build: 5
Built: 5 | failed: 0
Nodes per paper — min 71, median 190, max 369
```

```bash
papertrail classify     --corpus corpora/demo   # Stage B — BULK_MODEL, ~5 cheap calls
```
```
Section trees: 5 | already classified: 0 | to classify: 5
Classified: 5 | failed: 0
```

```bash
papertrail paper-pages  --corpus corpora/demo   # Stage C — BULK_MODEL, ~15 calls w/ retries
```
```
Section trees: 5 | already generated: 0 | to generate: 5
Generated: 5 | failed: 0
```
(This one took about 4.5 minutes for 5 papers × 3 headings each, real gateway latency
included — Stage C is the slowest step at this scale.)

```bash
papertrail topic-pages  --corpus corpora/demo   # Stage D — SYNTH_MODEL, ~4 calls
```
```
Facet groups: 3 | to generate: 3 | year pages: 0 (always regenerated)
Generated: 3 | failed: 0

Running lint...
```
(No output after "Running lint..." means it passed — a broken link would print
`BROKEN LINK in <file>: <link>` and the command would exit nonzero.)

```bash
papertrail index        --corpus corpora/demo   # Stage E — no LLM calls
```
```
Indexing full-text papers: 100%|██████████| 5/5
Indexed: 5 full-text papers + 0 stub docs = 5 docs, 982 nodes, 12 facet rows
```

## 4. Read the output — no server needed yet

```bash
cat corpora/demo/wiki/papers/2010ApJ...716L..36L.md
```
Frontmatter is fully deterministic (metadata + classification + computed related-papers
list); the body is the LLM-generated, cited prose:
```yaml
---
doc_id: 2010ApJ...716L..36L
title: DAY-SIDE z' -BAND EMISSION AND ECCENTRICITY OF WASP-12b*
year: null
journal: null
authors: null
fulltext_source: publisher_pdf
method:
- photometry
- theory-modeling
outcome: new-result
related_papers:
- doc_id: 2012ApJ...745...77K
  score: 3
- ...
generator:
  model: deepseek/deepseek-v4-flash-0731
  prompt_version: 2-3a1bea345d
  generated_at: '2026-08-10T07:52:47+00:00'
---

# DAY-SIDE z' -BAND EMISSION AND ECCENTRICITY OF WASP-12b*

## Overview
This paper reports the detection of the secondary eclipse of the hot Jupiter WASP-12b
via z'-band time-series photometry ... [1]. The measured eclipse depth is
0.082% ± 0.015% [1]. ...
```
Note `year`/`journal`/`authors` are all `null` — this corpus's minimal adapter
(`corpora/demo/adapter.py`) never supplies them, and nothing downstream requires it. The
`[n]` markers resolve via the companion sidecar:
```bash
cat corpora/demo/wiki/papers/2010ApJ...716L..36L.citations.json
```

## 5. Serve the web app

There is no `papertrail serve` subcommand — launch it as a plain ASGI app, pointed at
the corpus via an env var:

```bash
PAPERTRAIL_CORPUS=corpora/demo uvicorn papertrail.web.asgi:app --port 8011
```
```bash
curl http://127.0.0.1:8011/api/health
```
```json
{"status":"ok","corpus":"demo","doc_count":5}
```
```bash
curl http://127.0.0.1:8011/api/corpus
```
```json
{"name":"demo","title":"Demo Knowledge Base","glyph":"🧪",
 "facets":{"method":{"label":"Method","values":["photometry","spectroscopy","radial-velocity","theory-modeling"],"multi":true},
           "outcome":{"label":"Outcome","values":["new-result","review-or-synthesis"],"multi":false}},
 "navigation":{"tree_facet":"method","table_columns":["outcome"]}}
```
Branding and facet metadata come from `corpus.yaml` via this one endpoint — the packaged
SPA (open `http://127.0.0.1:8011/` in a browser) fetches it at boot rather than having
any corpus name baked into its HTML.

## 6. Connect the MCP server

```json
{"mcpServers": {"demo-kb": {"command": ".venv/bin/papertrail",
                             "args": ["mcp", "--corpus", "corpora/demo"]}}}
```
Called with no arguments, `list_papers()` teaches an agent the corpus's own vocabulary —
this is the real output for `corpora/demo`:
```
5 papers indexed (5 with full text, 0 metadata-only). Filter values —
- method: photometry, spectroscopy, theory-modeling
- outcome: new-result
- year:
```
Compare that to what the same tool call reports against this repo's main corpus
(`research_theme`/`method`/`facility`/`role_of_wasp12b`, ~200 papers) — same code, same
tool, different `corpus.yaml`.

The full authoring loop an agent should follow (search → read → cite → validate →
publish) is in [USERS_GUIDE.md §10](USERS_GUIDE.md#10-running-the-mcp-server).

## Where to go next

- **Bringing your own corpus** → [USERS_GUIDE.md](USERS_GUIDE.md), task-organized,
  starting from "what a corpus directory is."
- **Looking up an exact flag/field/endpoint/tool signature** →
  [REFERENCE.md](REFERENCE.md).
- **Modifying papertrail itself** → [DEVELOPERS_GUIDE.md](DEVELOPERS_GUIDE.md).
