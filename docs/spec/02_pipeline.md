# Pipeline

Five stages, `kb/build_sections.py` → `kb/build_index.py`, run in order. All resumable
and idempotent (skip work whose output is newer than its input and whose generator
provenance matches, matching `paper_download/01_fetch_pdfs.py` /
`kasi_papers_analysis/kb/04_paper_pages.py`'s conventions) with `--dry-run / --force /
--only DOC_ID / --limit N` on every stage. Schemas for every artifact are in
`01_data_contracts.md`; this document covers CLI, cost, and stage-specific algorithm
notes not already covered there.

**No reconversion of the source PDFs is required.** The original plan assumed
`02_convert_marker.py` would need re-running with image extraction enabled — verified
instead that Datalab's hosted API embeds full base64 image data in every `Figure`
block's `images` field *regardless* of the `disable_image_extraction` flag (confirmed:
269/269 Figure blocks in a 40-file sample). Stage A reads the existing 190
`paper_download/data/marker/*.json` files as-is.

## Run order

```bash
.venv/bin/python kb/build_sections.py     # Stage A
.venv/bin/python kb/classify.py           # Stage B
.venv/bin/python kb/paper_pages.py        # Stage C
.venv/bin/python kb/topic_pages.py        # Stage D
.venv/bin/python kb/build_index.py        # Stage E
```

`kb/config.py` is the shared seam every stage imports, mirroring
`paper_download/config.py`'s existing shape:

```python
ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")
BASE_URL = os.getenv("BASE_URL", "")
API_KEY = os.getenv("API_KEY", "")
BULK_MODEL = os.getenv("BULK_MODEL", "deepseek/deepseek-v4-flash-0731")
SYNTH_MODEL = os.getenv("SYNTH_MODEL", "anthropic/claude-opus-4.8")
CORPUS_YAML = Path(__file__).parent / "corpus.yaml"
KB_DIR = Path(__file__).parent
DATA_DIR = KB_DIR / "data"
SECTIONS_DIR = DATA_DIR / "sections"
ASSETS_DIR = DATA_DIR / "assets"
CLASSIFICATION_DIR = DATA_DIR / "classification"
KB_SQLITE = DATA_DIR / "kb.sqlite"
WIKI_DIR = KB_DIR / "wiki"

def load_corpus_config() -> dict: ...   # parses corpus.yaml
def load_papers() -> list[dict]: ...    # dynamically imports corpus.yaml's metadata_adapter
```

## Stage A — `build_sections.py`

**Input**: `paper_download/data/marker/<doc_id>.json` (190 files, existing).
**Output**: `kb/data/sections/<doc_id>.json` + `kb/data/assets/<doc_id>/*.jpeg`.

Algorithm is fully specified in `01_data_contracts.md` (block-type mapping table,
stack-based section assembly, Caption-folding, ListGroup `<li>`-parsing). No LLM calls —
fully deterministic, like kasi's `03_extract_text.py`.

```bash
.venv/bin/python kb/build_sections.py --dry-run
.venv/bin/python kb/build_sections.py                  # all 190
.venv/bin/python kb/build_sections.py --only 2013Icar..225..432S
.venv/bin/python kb/build_sections.py --force           # e.g. after fixing a bug in Stage A itself
```

Idempotency: skip a doc when `sections/<doc_id>.json` exists and its
`source_marker_sha256` matches the current marker file's hash (so re-running after a
Marker JSON is manually replaced — e.g. `RPPh`'s arXiv substitute already went through
this once at the PDF level — correctly reconverts).

Report at the end (kasi `03`'s pattern): min/median/max node count per paper; papers
with zero `reference` nodes (candidate ListGroup-parsing failures — should be near-zero
given every ListGroup so far has been a bibliography); any paper whose `page_count` from
the marker file exceeds 60 (the same wrong-document heuristic used during PDF
acquisition, worth re-checking here since it's cheap and catches anything that slipped
through).

Cost: free (no API calls).

## Stage B — `classify.py`

**Input**: Stage A's section trees (title + full text, truncated) + `corpus.yaml`'s
taxonomy. **Output**: `kb/data/classification/<doc_id>.yaml`.

One `BULK_MODEL` call per paper. Prompt: the taxonomy's facet names + closed value
lists (from `corpus.yaml`) + the paper's title and first ~4000 words of body text
(enough for the model to determine theme/method/facility/role without the full paper —
this is a coarse tag, not a citation-bearing claim, so truncation is safe here in a way
it wouldn't be for Stage C). Output format: structured (JSON mode if the gateway
supports it for `deepseek/deepseek-v4-flash-0731`; otherwise a fenced-YAML-block
convention with a parse-and-retry loop, same fallback kasi uses elsewhere for models
without native JSON mode).

Validation: every returned facet value must be in the corpus.yaml closed vocabulary
(reject and retry on a hallucinated value — cheap because `BULK_MODEL` is fast); `role_
of_wasp12b` must be exactly one value.

```bash
.venv/bin/python kb/classify.py --dry-run
.venv/bin/python kb/classify.py
.venv/bin/python kb/classify.py --only <doc_id> --force
```

The 20 stub docs (no fulltext) are skipped — classification needs body text.

Cost: low. `BULK_MODEL` is the cheap/fast tier by design (decision in `00_overview.md`
lineage); ~190 short calls.

## Stage C — `paper_pages.py`

**Input**: Stage A section tree + Stage B classification + corpus metadata.
**Output**: `kb/wiki/papers/<doc_id>.md` + `kb/wiki/papers/<doc_id>.citations.json`.

This is the stage where the unified citation contract (`00_overview.md`) actually gets
enforced. For each required heading in `corpus.yaml`'s `wiki_headings`:

1. Prompt `BULK_MODEL` with the paper's node list (each node's `node_id` + a short text
   preview, not full text for every node — keeps the prompt bounded even for
   long papers) and asks for that heading's prose **with `[n]` markers**, plus a
   `citations` list mapping each `n` used to a `node_id` from the supplied list.
2. Validate: (a) the heading's prose is non-empty and on-topic (existing kasi
   heading-presence check, generalized per-heading rather than "all 7 present at once"
   since each heading is now its own citation-bearing unit); (b) every cited `node_id`
   is actually in this paper's node list (cheap check — the model can only cite what it
   was shown, so a bad citation here means it invented one, not a real accuracy risk);
   (c) at least one citation present (a heading with zero citations is treated as
   "paper doesn't address this," matching kasi's "if a section is not addressed, write
   one sentence saying so" — accepted, but flagged in the report).
3. Retry up to 2 times with backoff on failure (kasi's pattern), assembling the whole
   page's `citations.json` by re-numbering each heading's local citation ids into one
   paper-wide sequential list (since the payload schema's citation ids are scoped per
   *result*, and a paper page is naturally one multi-section result).

Deterministic parts, unchanged from kasi's approach: `related_papers()` (shared-facet
scoring, adapted to the new taxonomy: `2×|shared research_theme| + 1×same
role_of_wasp12b + 2×|shared facility|`), `links_footer()`, frontmatter assembly.

```bash
.venv/bin/python kb/paper_pages.py --dry-run
.venv/bin/python kb/paper_pages.py
.venv/bin/python kb/paper_pages.py --only <doc_id> --force
.venv/bin/python kb/paper_pages.py --frontmatter-only    # kasi's free metadata-refresh path
.venv/bin/python kb/paper_pages.py --regenerate-if-prompt-changed
```

`--frontmatter-only` re-derives frontmatter + `## Links` from current
metadata/classification while preserving the existing body + citations.json untouched —
same zero-cost-refresh rationale as kasi's `04_paper_pages.py:225`.

Cost: moderate — 190 papers × 6 headings, each a `BULK_MODEL` call with retries. Still
well under $1 in aggregate given `BULK_MODEL`'s pricing tier, per kasi's own
"< $1 for flash-lite, 8 workers" experience at similar volume; validate the actual
`deepseek/deepseek-v4-flash-0731` pricing once billing is visible and update this
estimate.

## Stage D — `topic_pages.py`

**Input**: all Stage C paper pages + their classification facets.
**Output**: `kb/wiki/topics/<kind>-<slug>.md` + `.citations.json`, `kb/wiki/index.md`.

Page taxonomy (mirrors kasi's `group_papers()`, using this corpus's facets):

| page | source | citation treatment |
|---|---|---|
| `topics/theme-<slug>.md` | every `research_theme` value that occurs | cited (SYNTH_MODEL) |
| `topics/method-<slug>.md` | every `method` value | cited (SYNTH_MODEL) |
| `topics/facility-<slug>.md` | every `facility` value | cited (SYNTH_MODEL) |
| `topics/year-<yyyy>.md` | all years | programmatic, uncited |
| `index.md` | aggregate counts across all facets | cited (SYNTH_MODEL), high-level |

No author-topic pages here (no institutional-author angle in this corpus, unlike kasi's
KASI-author pages) — `role_of_wasp12b` doesn't warrant its own topic page (it's a
filter, not a browsing axis with interesting variation to summarize).

Synthesis call, `SYNTH_MODEL`: context = each member paper's Overview +
WASP-12b-Specific Findings sections (already-cited prose from Stage C) plus, for every
`node_id` those sections cite, the underlying node text fetched fresh from `kb.sqlite`
(so the topic page cites primary nodes, never another wiki page's citation — a citation
chain of depth >1 would break "click a citation, see the original text" for topic
pages). Prompt: closed set of `(doc_id, node_id)` pairs available to cite (the union of
all fetched nodes), same validate-and-retry loop as Stage C.

```bash
.venv/bin/python kb/topic_pages.py --dry-run
.venv/bin/python kb/topic_pages.py
.venv/bin/python kb/topic_pages.py --force
.venv/bin/python kb/topic_pages.py --lint   # exits 1 on any problem, doesn't regenerate
```

Lint (kasi's `05_topic_pages.py --lint`, extended): every fulltext-bearing paper has a
page; every required heading present with ≥1 citation (or explicitly empty); every
citation resolves; every relative link in `wiki/**/*.md` resolves on disk; every paper
appears in ≥1 theme/method/facility group (an orphan here likely means Stage B produced
an empty taxonomy tag — worth surfacing, not silently accepting).

Cost: low-volume (≈10 theme + 5 method + 7 facility + ~6 year + index ≈ 30 pages) but
each call is `SYNTH_MODEL` (Opus) with retries for citation validity — kasi's own
experience at similar volume was $5-15; expect similar here.

## Stage E — `build_index.py`

**Input**: Stage A section trees, Stage C/D wiki pages + citations.json, corpus
metadata (including the 20 stub docs and their `docs.fulltext_source = NULL` rows).
**Output**: `kb/data/kb.sqlite` (full drop-and-rebuild, kasi's "no resume logic to
maintain" rationale still holds at 210 total doc records).

- `docs` — one row per doc_id (190 full + 20 stub), including `fulltext_source`.
- `nodes` — one row per section-tree node (container + leaf), text as specified in
  `01_data_contracts.md`. **No resplitting step** (kasi's `resplit_long`/1500-word
  paragraph-boundary heuristic has no equivalent here — nodes are already
  paragraph/table/figure-sized, so `chunks_fts` indexes `nodes.text` directly).
- `chunks_fts` — FTS5, external-content, `porter unicode61` tokenizer (kasi's exact
  config), populated in one shot after `nodes` is fully loaded.
- `answers`/`conversations`/`messages` — created empty; the web app and MCP server
  populate these at runtime, `build_index.py` never writes to them (a full rebuild must
  not destroy chat history — see the migration note below).
- Wiki pages' `## Links` footer is stripped before indexing into `nodes` (kasi's
  rationale: link text is a keyword magnet that outranks real content in BM25 — still
  applies) — but wiki page **bodies themselves are not separately indexed as
  `nodes` rows**, since their prose already exists as a fulltext node (the same
  `(doc_id, node_id)` it cites) elsewhere in `nodes` for the underlying paper. Indexing
  the wiki prose too would mean two different rows can satisfy the same search hit with
  different provenance; the search index stays scoped to primary-source nodes only.
  Wiki *pages themselves* remain browsable via `read_wiki`/`GET /api/wiki/...`
  independent of the FTS index.

```bash
.venv/bin/python kb/build_index.py
```

No flags beyond none-needed — always a full rebuild, matching kasi. **Migration
concern**: since `build_index.py` drops and recreates `kb.sqlite` wholesale, and
`answers`/`conversations`/`messages` live in the same file, a naive `DROP TABLE`-all
would destroy chat history on every pipeline re-run. Mitigation: `build_index.py`
`ATTACH`es a fresh temp database for `docs`/`nodes`/`chunks_fts`, builds those tables
there, then swaps only those three (`DROP` + rename) inside a transaction, leaving
`answers`/`conversations`/`messages` in the original file untouched throughout.

Cost: free (no API calls).

## Cost summary

| Stage | API calls | Estimated cost |
|---|---|---|
| A — build_sections | none | free |
| B — classify | ~190 (BULK_MODEL) | low (<$1 expected, verify actual pricing) |
| C — paper_pages | ~190 × 6 headings (BULK_MODEL, w/ retries) | low–moderate |
| D — topic_pages | ~30 (SYNTH_MODEL, w/ retries) | moderate ($5-15 range, kasi precedent) |
| E — build_index | none | free |

Update this table with actual observed costs after the first full run — the
`BULK_MODEL` swap to `deepseek/deepseek-v4-flash-0731` (vs. kasi's
`gemini-2.5-flash-lite`) is untested at volume; if Stage B/C costs surprise, that's the
first place to look.
