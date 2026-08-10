# Data Contracts

Every schema in the system, in the order data flows through the pipeline. `02_pipeline.md`
describes how each artifact is produced; this document is the reference for its shape.

**Migration note:** the schemas below (`corpus.yaml` shape, `kb.sqlite` DDL, citation
field names) describe the **live** `kb/`/`webapp/` system and are still accurate for it
today. The `papertrail` extraction (see `CLAUDE.md`, `05_adoption.md`) generalized every
one of these — a generic `doc_facets` table instead of one column per facet,
`corpus.yaml` gaining `identity`/`taxonomy`-per-facet-config/`navigation`/`corpus.title`
etc. blocks, and citation payloads emitting `reference_label`/`external_links` instead of
the `author_year`/`ads_url` fields shown below — verified equivalent for wasp12b but not
yet cut over. Where it matters, a callout below points at the generalized shape; treat
this document as the contract for what's actually running until that cutover happens.

## `kb/corpus.yaml` — the generalization seam

One file that parameterizes everything corpus-specific. `05_adoption.md` is essentially
"how to write a new one of these."

```yaml
corpus:
  name: wasp12b
  description: Papers mentioning WASP-12 b

source:
  marker_dir: ../paper_download/data/marker      # existing Marker JSON output
  metadata_adapter: kb.adapters.wasp12b:load     # module:function, see below
  doc_id_field: safe_bibcode                      # how doc_id is derived

# Facets an LLM tags each paper with (kb/classify.py, Stage B). Each facet's
# `values` are shown to the model as the closed vocabulary; `multi: true` allows
# more than one value per paper.
taxonomy:
  research_theme:
    multi: true
    values: [orbital-decay, atmosphere-composition, atmosphere-escape-mass-loss,
             tidal-interaction, transit-timing, transit-spectroscopy,
             radial-velocity, magnetic-field, formation-evolution,
             survey-catalog-mention]
  method:
    multi: true
    values: [photometry, spectroscopy, radial-velocity, theory-modeling, radio]
  facility:
    multi: true
    values: [HST, Spitzer, JWST, TESS, Kepler, ground-based, theoretical-only]
  role_of_wasp12b:
    multi: false
    values: [primary-target, incidental-mention]

# Required H2 headings for a paper wiki page (kb/paper_pages.py, Stage C). Each
# heading's prose must carry [n] citations resolving to that paper's own nodes
# (see "Wiki page contract" below) — enforced by validate-and-retry.
wiki_headings:
  - Overview
  - Observations & Data
  - Method
  - Results
  - WASP-12b-Specific Findings
  - Limitations & Open Questions

models:
  bulk_env: BULK_MODEL      # paper pages + classification
  synth_env: SYNTH_MODEL    # topic pages
  chat_env: CHAT_MODEL      # interactive chat (webapp only, not read by the pipeline)
```

### Metadata adapter interface

`metadata_adapter` points at a function yielding one record per source document, before
any Marker/classification data is attached:

```python
def load() -> list[dict]:
    """Each dict: {doc_id, title, authors, year, journal, extra}.
    `extra` is adapter-specific and passed through untouched into corpus.yaml's
    consumers' prompts (e.g. this corpus's `extra['ads_url']`, `extra['doi']`)."""
```

This corpus's adapter (`kb/adapters/wasp12b.py`) wraps the existing
`paper_download/config.load_papers()` (reads `wasp_12b_papers.yaml`, joins in
`paper_download/data/ads_meta.json` for DOIs) — no new metadata-loading code, just a
thin re-export with `doc_id = safe_bibcode`. See `05_adoption.md` for what a from-scratch
adapter looks like for a corpus with no existing loader.

*(Migration note: `papertrail`'s version of this same adapter,
`corpora/wasp12b/adapter.py`, additionally supplies `native_id`/`has_fulltext`/
`fulltext_source` per record — see `05_adoption.md`'s checklist step 2 — and is loaded via
`importlib.util.spec_from_file_location` rather than a bare `module:function` dotted
path, so a corpus directory is self-contained without a `sys.path` hack.)*

---

## Section-tree JSON — `kb/data/sections/<doc_id>.json`

The primary extraction artifact and the thing every citation ultimately points into.
Navigator-compatible shape (`doc_name`, `structure[]` of nested nodes), extended with
paragraph/table/figure granularity and provenance fields the navigator didn't need.

### Node shape

Two kinds of node, distinguished by `kind`:

```
Container node (from a Datalab SectionHeader block):
  { "node_id": "0007", "kind": "container", "title": "3. RESULTS", "level": 2,
    "nodes": [ ...children... ] }
    # No "text" field — containers hold only their own heading. This preserves the
    # navigator's invariant that parent and child text are disjoint (concatenating
    # every node in document order reconstructs the paper with no duplication).

Leaf node (from any other Datalab block):
  { "node_id": "0008", "kind": "leaf", "block_type": "paragraph",
    "text": "...", "page": 6, "source_block_ids": ["/page/6/Text/2"] }
```

`level` on a container = the heading's HTML level (`<h1>`→1 … `<h6>`→6, same
`heading_level()` regex kasi's `03_extract_text.py` uses). `node_id` is a sequential
4-digit zero-padded counter assigned in document order across the whole tree (kasi
`pdf2json_v2.py`'s convention) — stable as long as the source Marker JSON doesn't
change, which is the reconversion-idempotence guarantee `02_pipeline.md` Stage A relies
on.

### `block_type` values and their `text`/extra fields

| `block_type` | From Datalab block | `text` | extra fields |
|---|---|---|---|
| `paragraph` | `Text` (verified: always exactly one `<p>`) | cleaned inline markdown (kasi's `InlineCleaner`: `<i>/<em>`→`*`, `<b>/<strong>`→`**`, `<sup>/<sub>` kept as literal HTML, `<math>`→`$…$`) | — |
| `equation` | `Equation` | `$$\n<latex>\n$$`, LaTeX pulled from the `<math display="block">` span | — |
| `table` | `Table` | the raw `<table>` HTML, verbatim | `caption`: text of the immediately-preceding `Caption` block, if any (verified: Captions consistently precede their Table/Figure sibling in document order — folded in rather than made a separate node) |
| `figure` | `Figure` | caption text (same folding rule as `table`) | `image_asset: "assets/<doc_id>/<block_id>.jpeg"` — see [Image assets](#image-assets) below |
| `footnote` | `Footnote` | cleaned inline text | — |
| `reference` | one `<li>` inside a `ListGroup`'s `html` | cleaned inline text of that one entry | `list_index`: position within the original list — **this is the fix for a real bug found in `kasi_papers_analysis`**: Datalab's hosted API returns `ListGroup` blocks with **zero children**, the entire `<ol>`/`<ul>` sitting in the block's `html` field; kasi's extractor treats `ListGroup` as a pure container (recurses into `children`) and so silently drops every bibliography (verified there: 121/121 ListGroups had zero children; verified again on this corpus: 488/488). This extractor instead parses `<li>` items out of the `html` directly, each becoming its own `reference` leaf node — bibliography entries become individually citable, which also fixes kasi's dropped-`## References`-section problem for free. |

Dropped entirely (no node, matching kasi's `DROP_TYPES`): `PageHeader`, `PageFooter`,
`Page` (structural, not content), `Picture` (a bare image with no caption — see note
below). `TableOfContents` (appeared once across the whole 190-paper corpus) is dropped
as noise. `Code` is handled if it ever appears (fenced block) though it never does in
this corpus (astronomy papers, no listings) — kept for adoption to other corpora.

`Picture` blocks (100 across this corpus, vs. 1683 `Figure`) are genuinely uncaptioned
images (a logo, a decorative element) — dropped rather than given a node, since a
citation to "an uncaptioned picture with no surrounding context" isn't useful. If this
proves wrong in practice (a `Picture` turns out to matter), promoting it to its own node
is a one-line change to the same handling as `figure`.

### Block-type census, this corpus (190 marker files, ground truth for the table above)

```
16681  Text            →  paragraph nodes
 4402  PageHeader      →  dropped
 3472  SectionHeader   →  container nodes
 2872  PageFooter      →  dropped
 2523  Page            →  dropped (structural)
 2342  Caption         →  folded into following table/figure node
 1823  Equation        →  equation nodes
 1683  Figure          →  figure nodes
  728  Footnote        →  footnote nodes
  671  Table           →  table nodes
  488  ListGroup       →  exploded into reference nodes (one per <li>)
  100  Picture         →  dropped
    1  TableOfContents →  dropped
```

Block types that appear in kasi's defensive code (`TextInlineMath`, `ListItem`, `Line`,
`Span`, `TableCell`, `TableGroup`, `FigureGroup`, `PageNumber`) **never occur** in this
corpus either — Datalab's hosted-API JSON output is consistently flatter than what
those branches were written to handle (they match the *local* `marker` Python library's
tree shape, not the hosted REST API's). Kept as a documented non-issue, not implemented.

### Assembly algorithm (Stage A)

Stack-based, one pass over each Page's flat child-block list (adapted from
`pdf2json_v2.py`'s `_assemble_sections`):

1. Maintain a stack of open containers, seeded with a synthetic root.
2. For each block in document order: if it's a `SectionHeader`, pop the stack until its
   top has `level < this heading's level`, push a new container node onto it (and onto
   the stack).
3. Any other block type becomes a leaf node (per the table above) appended to the
   current stack top's `nodes[]`. A `Caption` block is buffered, not emitted, until the
   next block is checked — if that next block is `Table` or `Figure`, its text becomes
   the `caption` field; otherwise (rare — a caption with no following figure/table) it
   is emitted as its own `paragraph`-typed leaf node so it isn't silently lost.
4. `ListGroup` blocks: parse `<li>` elements from `html` (BeautifulSoup or
   `html.parser`), emit one `reference` leaf per item.

### Worked example

Derived from real blocks in `paper_download/data/marker/2009ApJ...700.1921I.json`
(Ibgui & Burrows 2009, *Coupled Evolution with Tides...*):

```json
{
  "doc_id": "2009ApJ...700.1921I",
  "doc_name": "Coupled Evolution with Tides of the Radius and Orbit of Transiting Giant Planets: General Results",
  "source_marker_sha256": "<sha256 of the marker JSON file, for reconversion idempotence>",
  "page_count": 6,
  "structure": [
    { "node_id": "0001", "kind": "leaf", "block_type": "paragraph",
      "text": "LAURENT IBGUI AND ADAM BURROWS", "page": 0,
      "source_block_ids": ["/page/0/Text/1"] },
    { "node_id": "0002", "kind": "container", "title": "ABSTRACT", "level": 2,
      "nodes": [
        { "node_id": "0003", "kind": "leaf", "block_type": "paragraph",
          "text": "Some transiting extrasolar giant planets (EGPs) have measured radii larger than predicted by the standard theory...",
          "page": 0, "source_block_ids": ["/page/0/Text/4"] }
      ]},
    { "node_id": "0004", "kind": "container", "title": "1. INTRODUCTION", "level": 2,
      "nodes": [
        { "node_id": "0005", "kind": "leaf", "block_type": "paragraph",
          "text": "The most useful experimental data constraining evolutionary models of extrasolar planets and their radii ($R_p$) come from measurements of transiting planets.<sup>1</sup>",
          "page": 0, "source_block_ids": ["/page/0/Text/5"] },
        { "node_id": "0006", "kind": "leaf", "block_type": "footnote",
          "text": "See J. Schneider's Extrasolar Planet Encyclopaedia at http://exoplanet.eu, the Geneva Search Programme at http://exoplanets.eu.",
          "page": 0, "source_block_ids": ["/page/0/Footnote/0"] }
      ]}
  ]
}
```

And, from `2009A+A...506..399L.json`, a `table` node showing the caption-folding rule
(real block adjacency: a `Caption` block reading `<b>Table 1.</b> EUV related heating
processes...` immediately precedes the `Table` block):

```json
{ "node_id": "0031", "kind": "leaf", "block_type": "table",
  "caption": "Table 1. EUV related heating processes and heating efficiencies η.",
  "text": "<table><tr><th>Heating process</th><th>η [%]</th></tr>...</table>",
  "page": 3, "source_block_ids": ["/page/3/Caption/0", "/page/3/Table/0"] }
```

## Image assets

`kb/data/assets/<doc_id>/<block_id_slug>.jpeg` — one file per `figure`-typed node.
`block_id_slug` = the Marker block id with `/` replaced by `_` (e.g.
`/page/4/Figure/3` → `page_4_Figure_3.jpeg`), so it's traceable back to
`source_block_ids` without a separate lookup table.

**Verified fact, important for `02_pipeline.md` Stage A**: despite all 190 existing
Marker conversions being run with `disable_image_extraction: true`, every `Figure`
block's `images` field already carries the full base64-encoded image data (confirmed:
269/269 Figure blocks in a 40-file sample). Stage A decodes these directly — **no
reconversion of the 190 PDFs is needed**; this is a pure extraction step over data
already on disk.

## Classification sidecar — `kb/data/classification/<doc_id>.yaml`

```yaml
doc_id: 2013Icar..225..432S
generator: {model: deepseek/deepseek-v4-flash-0731, prompt_version: '1',
            generated_at: '2026-08-06T00:00:00+00:00'}
research_theme: [atmosphere-composition, transit-spectroscopy]
method: [spectroscopy]
facility: [ground-based]
role_of_wasp12b: primary-target
confidence: high
evidence: >
  Title and abstract explicitly target WASP-12b's atmosphere via transmission
  spectroscopy; Results section reports detection constraints for the system.
```

`confidence`/`evidence` mirror kasi's `ai_classification` shape (`confidence`,
`evidence`, `reasoning` fields) — self-documenting output that a human can audit without
re-running the model.

## Wiki page contract

### Paper pages — `kb/wiki/papers/<doc_id>.md`

Frontmatter is entirely deterministic (metadata + classification sidecar +
`fulltext_source`); body is the **unified answer-payload content** (see
"The unifying principle" in `00_overview.md`) — each required heading's prose carries
real `[n]` citations resolving to nodes in that paper's own section tree.

```markdown
---
doc_id: 2013Icar..225..432S
title: Probing the extreme planetary atmosphere of WASP-12b
year: 2013
journal: Icarus
authors: [Sing, D. K., ...]
fulltext_source: publisher_pdf   # or arxiv_preprint (only 2010RPPh...73a6901B)
research_theme: [atmosphere-composition, transit-spectroscopy]
method: [spectroscopy]
facility: [ground-based]
role_of_wasp12b: primary-target
related_papers: [{doc_id: 2012ApJ...750..100C, title: "...", score: 4}, ...]
generator: {model: deepseek/deepseek-v4-flash-0731, prompt_version: '1',
            generated_at: '2026-08-06T00:00:00+00:00'}
---

# Probing the extreme planetary atmosphere of WASP-12b

## Overview
This paper reports transmission spectroscopy of WASP-12b targeting evidence of an
extended atmosphere shaped by its extreme irradiation [1][2].

## Observations & Data
...

## Method
...

## Results
The transit depth measurements show ... [7], consistent with an inflated atmosphere
at optical wavelengths [8].

## WASP-12b-Specific Findings
...

## Limitations & Open Questions
...

## Links
- Themes: [atmosphere-composition](../topics/theme-atmosphere-composition.md) | [transit-spectroscopy](../topics/theme-transit-spectroscopy.md)
- Facility: [ground-based](../topics/facility-ground-based.md)
- Related papers: [2012ApJ...750..100C](2012ApJ...750..100C.md) "...", ...
- External: [ADS](https://ui.adsabs.harvard.edu/abs/2013Icar..225..432S) | [DOI](https://doi.org/...)

<!-- citations: see 2013Icar..225..432S.citations.json -->
```

The `[n]` → `(doc_id, node_id)` mapping for the body is stored alongside the page as
`kb/wiki/papers/<doc_id>.citations.json` — a plain `citations[]` array per the
[answer payload schema](#answer-payload-schema) below — rather than embedded in the
markdown, so the wiki page stays human-readable while remaining machine-checkable
(`validate_answer` reads the sidecar, not the markdown, when auditing a page).
`related_papers`/`## Links` are deterministic (kasi's scoring: shared taxonomy
facets + shared "role" weighting, adapted from kasi's shared-technique/shared-author
scoring), never LLM-authored.

For the 2 non-`publisher_pdf` special cases:
- `2010RPPh...73a6901B` gets `fulltext_source: arxiv_preprint` and a note in Overview
  acknowledging the source is the arXiv preprint of the review, not the IOP-typeset
  version.
- Papers with `has_fulltext: false` (the 20 stub docs, see below) get **no wiki page at
  all** — there's no text to cite, so a citation-bearing page can't be generated. They
  appear only as `docs` rows and in `list_papers`/search results.

### Topic pages — `kb/wiki/topics/<kind>-<slug>.md`

Same citation treatment, now citing across the topic's member papers. Context fed to
`SYNTH_MODEL` is each member paper's **Overview + WASP-12b-Specific Findings** sections
(their own already-cited prose, since a topic page benefits from what those sections
already established) plus each cited node's underlying text (fetched fresh so the topic
page's own citations point at primary content, not at other wiki prose — a topic page
never cites another wiki page's `node_id`, only paper-level section-tree nodes).

```markdown
---
title: 'Theme: Orbital Decay'
kind: theme
paper_count: 14
generator: {model: anthropic/claude-opus-4.8, generated_at: '...'}
---

# Theme: Orbital Decay

WASP-12b's continuously shrinking orbital period was first suspected from timing
residuals [1] and confirmed with high-cadence photometry a decade later [2]...

## Papers
| Year | Paper | Role | Facility |
|---|---|---|---|
| 2017 | [...](../papers/2017ApJ......md) | primary-target | ground-based |
...

[← KB index](../index.md)
```
`## Papers` stays deterministic (kasi's `member_table` pattern); programmatic
topic pages (by-year, by-author if that ever applies here) stay fully uncited, matching
kasi's `generator.model: "programmatic"` / no-timestamp convention for byte-stable
re-runs.

## `kb.sqlite` schema

**This is the live schema** — full drop-and-rebuild every run (kasi's explicit "no
resume logic to maintain" choice, still valid at this scale). `papertrail`'s generalized
schema (`src/papertrail/core/schema.py`, not yet cut over — see the migration note at
the top of this document) replaces `research_theme_json`/`method_json`/`facility_json`/
`role_of_wasp12b` with a generic `doc_facets(doc_id, facet, value)` table plus a
`docs.facets_json` display blob, and adds `docs.native_id`/`docs.extra_json`.

```sql
CREATE TABLE docs (
    doc_id TEXT PRIMARY KEY,
    title TEXT, year INTEGER, journal TEXT, authors_json TEXT,
    fulltext_source TEXT,       -- publisher_pdf | arxiv_preprint | NULL (stub docs)
    has_fulltext INTEGER NOT NULL,
    research_theme_json TEXT, method_json TEXT, facility_json TEXT,
    role_of_wasp12b TEXT
);

CREATE TABLE nodes (
    id INTEGER PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES docs(doc_id),
    node_id TEXT NOT NULL,        -- e.g. "0008" — unique within doc_id
    kind TEXT NOT NULL,           -- container | leaf
    block_type TEXT,              -- paragraph|equation|table|figure|footnote|reference|NULL(container)
    parent_title TEXT,            -- nearest enclosing container's title, for display/context
    page INTEGER,
    text TEXT NOT NULL,           -- container: its own title; leaf: its content
    UNIQUE (doc_id, node_id)
);

-- One row per leaf/container node's text — no resplitting needed (nodes are
-- already paragraph-sized; kasi's 1500-word resplit_long has no equivalent here).
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    text, content='nodes', content_rowid='id', tokenize='porter unicode61'
);

CREATE TABLE conversations (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE answers (
    id TEXT PRIMARY KEY,          -- opaque id, e.g. uuid4 hex
    kind TEXT NOT NULL,           -- chat_message | wiki_paper | wiki_topic | agent_adhoc
    conversation_id INTEGER REFERENCES conversations(id),   -- NULL unless kind=chat_message
    title TEXT,
    payload_json TEXT NOT NULL,   -- the full answer payload, see below
    created_at TEXT NOT NULL
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL,           -- user | assistant
    text TEXT,                    -- user question text; NULL for assistant (see answer_id)
    answer_id TEXT REFERENCES answers(id),   -- set for role=assistant
    created_at TEXT NOT NULL
);
```

`docs.fulltext_source IS NULL` marks the 20 stub entries — indexed with exactly one
`nodes` row (`kind='leaf', block_type='stub', text='<title>. <authors>. <journal> (<year>).'`)
so they're findable by title/author search without pretending to have real content.
`wiki_paper`/`wiki_topic` answers also get inserted into `answers` (not just written to
disk as markdown) so `GET /api/answers/{id}` and MCP's `get_paper`/`read_wiki` share one
retrieval path — the markdown files are the human-browsable rendering of the same rows.

## Answer payload schema

Extends `multi_doc_navigator`'s `docs/payload.schema.json` (reused verbatim for the
`result`/`citation` shapes) with the three new top-level fields this system's producers
(chat, pipeline, MCP) require: `id`, `kind`, `created_at`, and optional
`conversation_id`. The navigator's own consumer-side fields (`label`, `question`) are
unchanged.

```json
{
  "id": "a1b2c3d4",
  "kind": "chat_message",
  "conversation_id": 7,
  "created_at": "2026-08-06T00:00:00+00:00",
  "results": [
    {
      "question": "What orbital-decay rate do these papers converge on?",
      "answer": "Timing analyses converge on a period derivative around dP/dt ≈ -29 ms/yr [1], consistent with the tidal-decay interpretation reinforced by later photometry [2].",
      "citations": [
        {"id": 1, "doc_id": "2017ApJ...835...16C", "node_id": "0042"},
        {"id": 2, "doc_id": "2022ApJ...931...86H", "node_id": "0118"}
      ]
    }
  ]
}
```

*(Migration note: `papertrail`'s `validate_and_enrich` — now `src/papertrail/core/
citations.py`, not yet cut over — emits `reference_label` + `external_links[]` per
citation instead of this schema's `author_year`/`ads_url`, via a pluggable
`CitationStyle`; see `05_adoption.md` step 6. The webapp's stored-payload compat shim
reads either shape.)*

Validation rule set (extends the navigator's `validate_and_enrich`):
1. Structural — matches the JSON Schema (`anyOf[canonical, bare result]`,
   `additionalProperties: false`), same as the navigator.
2. Citation resolution — every `citations[].{doc_id,node_id}` pair must exist in
   `nodes`. Unlike the navigator (single-corpus, always-present docs), this system also
   checks `docs.has_fulltext = 1` for the cited `doc_id` — citing a stub doc is always
   an error, since stub docs have no real node content.
3. **Non-fatal by default** (`strict=false`, matching navigator semantics): unresolved
   citations move to `warnings[]`, the payload still stores/renders with the citation
   marker left in place but not clickable. `strict=true` (used by MCP's
   `validate_answer` during the agent's authoring loop) instead raises, giving the
   agent something to fix before publishing.

Two additional producer-specific rules layered on top for `kind=wiki_paper`:
required headings present (the `corpus.yaml` `wiki_headings` list) **and** each
required heading's prose contains at least one citation — enforced by
`kb/paper_pages.py`'s generate-and-retry loop, not by `validate_and_enrich` itself
(headings are a wiki-page-specific concept, not a generic answer-payload concept).

## Corpus completeness — the 20 stub `docs` rows

Concretely, each of the 20 fulltext-less entries from `wasp_12b_papers.yaml` becomes:

```sql
INSERT INTO docs (doc_id, title, year, journal, authors_json,
                   fulltext_source, has_fulltext, ...)
VALUES ('2012AAS...21340207M', 'Coupled Thermal and Tidal Evolution of Known Transiting Planets',
        2009, 'American Astronomical Society Meeting Abstracts #213',
        '["Miller, Neil", "Fortney, J.", "Jackson, B."]', NULL, 0, ...);
```
with a single `nodes` row of `block_type='stub'` carrying just
`"Coupled Thermal and Tidal Evolution of Known Transiting Planets. Miller, Neil; Fortney, J.; Jackson, B. (2009)."`
— enough for BM25 title/author matching, nothing more. `doc_id` for the 3 entries with
no bibcode in the source YAML (Gary 2009 ephemeris, Exoplanet Data Explorer, the 2025
A&A corrigendum) is synthesized from `slugify(title)` since `safe_bibcode` doesn't apply
— these are the only `doc_id`s in the whole system not derived from a bibcode.
