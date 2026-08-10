# Adopting `papertrail` for Another Corpus

**This doc was rewritten as part of the papertrail extraction
(`.claude/plans/i-would-like-to-stateless-hellman.md`).** Its previous version claimed
this toolchain was already corpus-agnostic — "no code changes needed for a different
corpus," prompts that "reference the taxonomy/headings from `corpus.yaml`, not
hardcoded." Verified against the actual `kb/`/`webapp/` code, both claims were false: all
four LLM prompts hardcoded "the exoplanet WASP-12b," Stage D never read `corpus.yaml` at
all, the sqlite schema had a literal `role_of_wasp12b` column, and the corpus title was
hardcoded in five separate places. That gap is the entire reason the `papertrail`
extraction exists — see the migration plan for the full account. This doc now describes
the *actual* adoption path once the extraction is complete, not an aspirational one.

**Status:** as of this writing, all 12 steps of the migration plan are done, including
Step 12 — `corpora/demo/` is a real, differently-shaped second corpus (2-facet taxonomy
vs. wasp12b's four, 3 wiki headings vs. six, a different `subject`, `citation_style:
plain`, no external links, a from-scratch filename-stem adapter instead of one wrapping
an existing loader) run end-to-end through Stages A–E and served over both the web app
and MCP server. The checklist below is now proven in the "a second, differently-shaped
corpus also works" sense, not just verified for wasp12b — and Step 12 caught one real
genericity bug in the process (`pipeline/index.py` had a leftover hardcoded facet-name
tuple that silently dropped any facet not in wasp12b's specific four; fixed, see that
commit). For the practical, task-organized version of this checklist plus a full
reference of every `corpus.yaml` key/CLI flag/API endpoint, see `USERS_GUIDE.md` and
`REFERENCE.md` at the repo root — this document remains the design-rationale account of
*why* each seam is shaped the way it is, not the primary how-to anymore.

## What's generic vs. corpus-specific, for real this time

| Generic (`src/papertrail/`, reuse as-is) | Corpus-specific (`corpora/<name>/`, write per corpus) |
|---|---|
| Section-tree node model, block-type mapping, Caption-folding, ListGroup `<li>`-parsing (Stage A) | `corpus.yaml`: taxonomy facets + values + weights, wiki heading list, navigation, identity, prompt subject |
| `kb.sqlite` schema (generic `doc_facets` table, not one column per facet), FTS5 indexing (Stage E) | Metadata adapter (`corpus.yaml`'s `metadata_adapter`) |
| Answer payload contract + `validate_and_enrich` | `identity.py`'s `CitationStyle` selection + `external_links` templates (only if the built-in `author_year`/`plain` styles don't fit) |
| MCP tool set (9 tools; `search_fulltext`/`list_papers`' facet parameters are built at runtime from `corpus.yaml`'s taxonomy, not hardcoded) | Classification/paper-page/topic-page *prompt content* — `corpus.yaml`'s `subject`/`citation_example`, or a `prompts_dir:` override for anything the two variables can't express |
| Web app (viewer, chat, `GET /api/corpus` branding, answers store) | Corpus branding (`corpus.yaml`'s `title`/`glyph`/`description`) |
| Classification/paper-page/topic-page *mechanics* (validate-and-retry loops, taxonomy-driven similarity scoring, taxonomy-driven topic-page generation) | — |

## Checklist

1. **Point Stage A at the new Marker JSON directory.** `corpus.yaml`'s `source.marker_dir`
   (corpus-relative). No code changes needed if the corpus was converted the same way
   (`output_format=json` against the Datalab API) — the block-type table in
   `01_data_contracts.md` is Datalab's hosted-API shape, not anything WASP-12b-specific.
   If image extraction was genuinely disabled for the new corpus, Stage A's image step
   just finds nothing to extract — not an error, `figure` nodes simply lack an
   `image_asset`.

2. **Write a metadata adapter.** `corpus.yaml`'s `source.metadata_adapter` is either a
   corpus-relative file path (`./adapter.py:load`, resolved via
   `importlib.util.spec_from_file_location` — no sys.path mutation, what a
   self-contained corpus directory should use) or a dotted module path
   (`some.installed.module:load`). Contract (`01_data_contracts.md`):
   ```python
   def load() -> list[dict]:
       """One dict per doc: {doc_id, title, authors, year, journal, extra}, optionally
       has_fulltext/fulltext_source/native_id/skip_reason if your corpus has them."""
   ```
   `corpora/wasp12b/adapter.py` is a ~90-line real example: it wraps an existing loader
   (`paper_download/config.load_papers()`), computes `has_fulltext`/`fulltext_source`
   from a PDF-fetch manifest, and supplies `native_id` (the pre-filesystem-escaping
   bibcode) for the identity seam below. For a corpus with **no** existing metadata file
   — just a directory of Marker JSONs — the minimal adapter derives everything from the
   Marker JSON itself: `doc_id` = filename stem, `title` = the first `SectionHeader`/
   `<h1>` block's text, `year`/`authors`/`journal` = `None`. A missing `identity:` block
   in `corpus.yaml` (see step 6) already tolerates this gracefully — no bibcode, no
   `native_id`, no external links rendered, labels fall back to `doc_id`/`title`.

3. **Choose a taxonomy.** `corpus.yaml`'s `taxonomy:` block, one entry per facet:
   `values` (closed vocabulary), `multi: true|false`, plus presentation config —
   `label` (display name), `topic_pages: true|false` (does this facet get its own
   cross-paper topic pages?), `slug_prefix` (topic-page filename prefix, must be unique
   among `topic_pages: true` facets), `similarity_weight` (how much this facet counts
   toward Stage C's related-papers scoring; `0` excludes it). No fixed number of facets.
   `navigation:` picks which facet groups the web app's doc tree (`tree_facet`) and which
   facets appear as columns in MCP's `list_papers` table (`table_columns`).

4. **Choose a wiki heading contract.** `corpus.yaml`'s `wiki_headings:` — a list of
   either bare strings or `{name, may_not_apply, guidance}` objects. `may_not_apply:
   true` on a heading feeds the paper-page prompt's "if this section genuinely doesn't
   apply, say so honestly" example; `guidance` isn't currently interpolated into the
   prompt (see the migration plan's Seam 2 notes for where that would go) but is
   reserved for it. `navigation.digest_headings` picks which heading(s) feed Stage D's
   cross-paper synthesis digest — this MUST be a subset of `wiki_headings`'
   names, checked at config-load time (a real bug this used to produce: renaming a
   heading without updating a hardcoded `DIGEST_SECTIONS` constant silently broke every
   topic page with a misleading "no member paper has a wiki page yet").

5. **Set the prompt subject and citation example.** `corpus.yaml`'s `corpus.subject`
   (a noun phrase, e.g. "the exoplanet WASP-12b") and `corpus.citation_example: {bad,
   good}` — interpolated into all five LLM system prompts (classify, paper-page,
   topic-page, index-intro, chat). The package's default prompt templates
   (`src/papertrail/prompts/*.txt`) still say "astronomy papers" and use a "C/O ratio"
   ambiguity example — override the whole template file via `prompts_dir:` (corpus-
   relative) if your domain needs different framing; there's no partial/inheritance
   mechanism, an override replaces a template wholesale.

6. **Set identity/citation style, if the defaults don't fit.** `corpus.yaml`'s
   `identity.citation_style` defaults to `author_year` (bibcode-shaped doc_id -> bare-
   year chip label, `"Last, First; ..."` authors -> `"Lastname et al. YYYY"` reference
   label, the author-year uncited-mention heuristic enabled). `plain` disables the
   author-year assumptions and the heuristic entirely — use it for a corpus with no
   author-year citation convention worth enforcing. `identity.external_links` is a
   declarative list of `{label, url}` templates (`{native_id}`, `{doc_id}`, `{year}`,
   `extra.*` fields available, percent-encoded by default, `{var:raw}` for an
   already-URL-safe value); a link whose referenced field is empty is silently dropped.
   For anything neither builtin covers (wasp12b needs one 6-line subclass just for its
   bibcode `&`/`+` filesystem-escaping), point `citation_style` at a corpus-relative
   `./style.py:ClassName` subclassing `papertrail.identity.AuthorYearStyle`.

7. **Set branding.** `corpus.yaml`'s `corpus.title`/`corpus.glyph`/`corpus.description`
   — served via `GET /api/corpus`, which the single-file SPA fetches at boot (no
   hardcoded corpus name anywhere in `index.html`).

8. **Run the pipeline** (`02_pipeline.md`, Stages A→E via `papertrail sections|classify|
   paper-pages|topic-pages|index --corpus corpora/<name>`) — genuinely no code changes
   for a different corpus now; only `corpus.yaml` + the adapter (+ an optional
   `style.py`/`prompts_dir` override) differ.

9. **Point the web app / MCP server at the new corpus** via `--corpus PATH` or the
   `PAPERTRAIL_CORPUS` env var (`03_webapp.md`'s Ops section) — same binary, no
   per-corpus fork of the server code. (The pre-migration `KB_DIR`/`KB_SQLITE`-only env
   vars are still honored for one release, but `--corpus`/`PAPERTRAIL_CORPUS` is the
   real mechanism now — see `papertrail.config.load_settings`.)

## What this spec still deliberately doesn't cover

A fully worked second example corpus — that's the migration plan's Step 12, tracked
separately, not duplicated here. The checklist above is written from real experience
building the papertrail extraction against wasp12b; the risk of a *hypothetical* second
example is inventing problems that don't actually occur and missing ones that do. Once
Step 12 lands (a real, differently-shaped demo corpus run end-to-end), this doc should
be updated with whatever it actually required that this checklist didn't anticipate.
