# Adopting This Toolchain for Another Marker Corpus

Thin by design — everything generic already lives in `01_data_contracts.md`
(section-tree/node schema, image assets, `kb.sqlite` schema, answer payload) and
`02_pipeline.md`/`03_webapp.md`/`04_mcp.md` (stage algorithms, endpoints, tools). None
of that references WASP-12b, bibcodes, or astronomy specifically — the only
corpus-specific surface is `kb/corpus.yaml` plus the metadata adapter it points at.

## What's generic vs. corpus-specific

| Generic (reuse as-is) | Corpus-specific (write per corpus) |
|---|---|
| Section-tree node model, block-type mapping, Caption-folding, ListGroup `<li>`-parsing (Stage A) | `corpus.yaml`: taxonomy facets + values, wiki heading list |
| `kb.sqlite` schema, FTS5 indexing (Stage E) | Metadata adapter (`corpus.yaml`'s `metadata_adapter`) |
| Answer payload contract + `validate_and_enrich` | Classification prompt content (facet meanings, what "counts" as each value) |
| MCP tool set (all 9) | Wiki heading semantics (what each required H2 should contain) |
| Web app (viewer, chat, answers store) | — |
| Classification/paper-page/topic-page *mechanics* (validate-and-retry loops) | Classification/paper-page/topic-page *prompts* (reference the taxonomy/headings from `corpus.yaml`, not hardcoded) |

## Checklist

1. **Point Stage A at the new Marker JSON directory.** No changes needed if the corpus
   was converted the same way (`output_format=json` against the Datalab API) — the
   block-type table in `01_data_contracts.md` is Datalab's hosted-API shape, not
   anything WASP-12b-specific. If image extraction was genuinely disabled for the new
   corpus (unlike this one, where it turned out not to matter), Stage A's image step
   just finds nothing to extract — not an error, `figure` nodes simply lack an
   `image_asset`.

2. **Write a metadata adapter.** Interface (`01_data_contracts.md`):
   ```python
   def load() -> list[dict]:
       """One dict per doc: {doc_id, title, authors, year, journal, extra}."""
   ```
   This corpus's adapter is a 10-line wrapper around an existing loader
   (`paper_download/config.load_papers()`). For a corpus with **no** existing metadata
   file — just a directory of Marker JSONs — the minimal adapter derives everything
   from the Marker JSON itself: `doc_id` = filename stem, `title` = the first
   `SectionHeader`/`<h1>` block's text, `year`/`authors`/`journal` = `None` (the wiki
   heading contract and taxonomy prompts should tolerate missing metadata gracefully —
   `corpus.yaml`'s prompts should say "if a metadata field is unavailable, omit it
   rather than guessing").

3. **Choose a taxonomy.** Ask: what axis would someone actually browse this corpus by?
   For WASP-12b it was research theme/method/facility/role-in-corpus. A different
   corpus (e.g. a general exoplanet-atmosphere literature review, not centered on one
   object) might not need a `role_of_wasp12b`-equivalent facet at all, or might need a
   `target_object` multi-value facet instead. No fixed number of facets — `corpus.yaml`
   accepts any number, each with `multi: true|false` + a closed `values` list.

4. **Choose a wiki heading contract.** Ask: what should every paper's page say,
   regardless of topic? WASP-12b's 6 headings (`01_data_contracts.md`) suit a corpus
   organized around one recurring subject. A pure methods-survey corpus might instead
   want headings like `Approach` / `Comparison to Prior Methods` / `Reported
   Performance` — the mechanics (validate-and-retry, per-heading citations) don't care
   what the headings are, only that `corpus.yaml` lists them and the generation prompt
   in `kb/paper_pages.py` explains what each should contain.

5. **Run the pipeline** (`02_pipeline.md`, Stages A→E, no code changes needed for a
   different corpus — only `corpus.yaml` and the adapter differ).

6. **Point `webapp`/`kb/mcp_server.py` at the new `kb.sqlite`/`corpus.yaml`** via
   `KB_DIR`/`KB_SQLITE` env overrides (`03_webapp.md`'s Ops section) — same binary, no
   per-corpus fork of the server code.

## What this spec deliberately doesn't cover

A fully worked second example corpus. The checklist above is written from real
experience building this one (WASP-12b) — the risk of a *hypothetical* second example
is inventing problems that don't actually occur and missing ones that do. Write the
worked second example after actually adopting the toolchain once, grounded in what that
adoption really required.
