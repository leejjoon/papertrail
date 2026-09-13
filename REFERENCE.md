# papertrail Reference

Pure lookup tables for the CLI, config, `corpus.yaml` schema, adapter/identity
contracts, on-disk layout, HTTP API, MCP tools, SQLite schema, answer payload shape, and
tunable constants — no narrative. Each section names its source file so it can be
re-verified directly against the code. For task-oriented "how do I..." documentation, see
[USERS_GUIDE.md](USERS_GUIDE.md); for a 15-minute working example, see
[QUICKSTART.md](QUICKSTART.md).

## Contents

1. [CLI](#1-cli)
2. [Environment variables](#2-environment-variables)
3. [`corpus.yaml` schema](#3-corpusyaml-schema)
4. [Adapter & `CitationStyle` interfaces](#4-adapter--citationstyle-interfaces)
5. [On-disk artifact layout](#5-on-disk-artifact-layout)
6. [HTTP API](#6-http-api)
7. [MCP tools](#7-mcp-tools)
8. [SQLite schema](#8-sqlite-schema)
9. [Answer payload schema](#9-answer-payload-schema)
10. [Tunable constants](#10-tunable-constants)

---

## 1. CLI

Source: `src/papertrail/cli.py`.

`cli.py` is a bare dispatch table, no argparse of its own: it reads `sys.argv[1]`, looks
it up below, and calls `module.main(sys.argv[2:])`.

| Subcommand | Dispatches to | Stage |
|---|---|---|
| `sections` | `papertrail.pipeline.sections` | A: Marker JSON → section-tree JSON |
| `classify` | `papertrail.pipeline.classify` | B: LLM taxonomy classification |
| `paper-pages` | `papertrail.pipeline.paper_pages` | C: cited wiki paper pages |
| `topic-pages` | `papertrail.pipeline.topic_pages` | D: cross-paper topic pages |
| `index` | `papertrail.pipeline.index` | E: build `kb.sqlite` |
| `mcp` | `papertrail.mcp.server` | run the MCP server |
| `ingest-pdf` | `papertrail.pipeline.ingest_pdf` | convert one PDF with Datalab, build sections, and refresh the index |
| `serve` | `papertrail.web.serve` | run the browser reader for an explicit corpus |

An unknown or missing subcommand prints `usage: papertrail <command> [args...]` plus this
list to stderr and exits `2`.

`ingest-pdf --init-corpus` creates a minimal manifest-backed `corpus.yaml` when missing.
`--corpus-title` customizes its browser title and is valid only with `--init-corpus`.
Existing corpus configuration is never overwritten.

**`run-all`/`lint`/`doctor` do not exist.** The web app may be launched via
`papertrail serve --corpus PATH` or directly via
`uvicorn papertrail.web.asgi:app` (§6); `topic-pages --lint` (below) covers the one thing
a standalone `lint` subcommand would.

### Per-stage flags

They are genuinely not identical — sources: `pipeline/sections.py`
(`build_arg_parser`), `pipeline/classify.py`, `pipeline/paper_pages.py`,
`pipeline/topic_pages.py`, `pipeline/index.py`.

| Flag | sections | classify | paper-pages | topic-pages | index |
|---|:-:|:-:|:-:|:-:|:-:|
| `--corpus PATH` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `--dry-run` | ✓ | ✓ | ✓ | ✓ | — |
| `--force` | ✓ | ✓ | ✓ | ✓ | — |
| `--only DOC_ID` | ✓ | ✓ | ✓ | — | — |
| `--limit N` | ✓ | ✓ | ✓ | — | — |
| `--workers N` | — | ✓ (openai `8`, codex `2`) | ✓ (openai `5`, codex `2`) | — | — |
| `--regenerate-if-prompt-changed` | — | ✓ | ✓ | — | — |
| `--frontmatter-only` | — | — | ✓ | — | — |
| `--lint` | — | — | — | ✓ | — |

`mcp` takes `--corpus`, plus `--transport {stdio,http,streamable-http}`, `--host`, and
`--port`. CLI values override the corresponding MCP environment variables.

**Per-flag semantics:**

- `--corpus PATH` → `load_settings(args.corpus, create_dirs=True)` in every pipeline
  stage (`create_dirs=True` mkdirs the corpus's `data/`/`wiki/` subdirectories if
  missing). `mcp` passes `create_dirs=False` (never creates directories, matching the web
  app).
- `--dry-run` — prints the computed to-do list (`  [dry-run] <doc_id>`, or for
  `topic-pages`, `  [dry-run] index.md` too) and returns with no LLM calls or writes.
- `--force`:
  - `sections`: bypasses the `already_done()` check (compares `source_marker_sha256`
    in the existing section-tree JSON against the current marker file's hash).
  - `classify`: treats every doc as `"missing"` regardless of stored `prompt_version`.
  - `paper-pages`: skips the `prompt_version` comparison entirely. **Mutually exclusive
    with `--frontmatter-only`** (`sys.exit` with an explanatory message if both given).
  - `topic-pages`: regenerates every topic page even if its `.md` already exists, and
    forces `index.md` regeneration too.
- `--only DOC_ID` — process exactly one `doc_id`, skipping everything else.
- `--limit N` — truncate the to-do list to the first `N` entries (after `--only`
  filtering, if both given).
- `--workers N` — `ThreadPoolExecutor(max_workers=N)`. Defaults are backend-aware as
  shown above; an explicit positive value always wins. `topic-pages` is deliberately
  serial (no `--workers`) since Stage D's synthesis calls are few and expensive.
- `--regenerate-if-prompt-changed` — without this flag, a doc whose stored
  `prompt_version` differs from the current rendered prompt's hash is counted as
  "done, but with a different prompt version" and left alone; with it, such docs are
  regenerated. Prevents an accidental corpus-wide re-run from a cosmetic prompt edit.
- `--frontmatter-only` (`paper-pages` only) — zero LLM calls, **no `API_KEY` needed**.
  Reloads each doc's existing wiki page, keeps its body markdown byte-for-byte, and
  recomputes only the frontmatter + `## Links` footer from current metadata/
  classification — preserving the existing `prompt_version` (an untouched body isn't a
  "regenerated" page). Only touches docs that already have both a classification and an
  existing `wiki/papers/<doc_id>.md`.
- `--lint` (`topic-pages` only) — runs the link-checker and exits immediately: walks
  every `wiki_dir/**/*.md`, resolves every relative markdown link against its containing
  file, prints `BROKEN LINK in <file>: <link>` for anything unresolved, exits `0` if
  clean or `1` otherwise.

**Two non-obvious behaviors:**

1. A normal (non-`--lint`) `topic-pages` run **always** runs the same lint check at the
   end and exits with its result — a successful generation that happens to leave a
   broken wiki link exits `1`, not `0`.
2. With `PIPELINE_BACKEND=openai`, `API_KEY` is required by `classify`, `paper-pages`
   (unless `--frontmatter-only`), and `topic-pages` (unless `--lint`). Codex mode uses
   the cached `codex login` instead. `sections` and `index` never need model credentials
   — they're pure/deterministic.

---

## 2. Environment variables

Source: `src/papertrail/config.py`.

### Corpus resolution — `_find_corpus_root()`

Strict precedence order, no implicit fallback to a bundled corpus:

1. `--corpus PATH` (if the caller passed one explicitly).
2. `PAPERTRAIL_CORPUS`, else `KB_DIR` (legacy alias, kept for one release).
3. Upward search from `cwd` — the nearest ancestor directory (starting at `cwd` itself)
   containing a `corpus.yaml` wins.
4. Otherwise: a hard `CorpusConfigError` naming all three options.

Each of steps 1–2 additionally errors immediately if the resolved directory has no
`corpus.yaml` — it does not silently fall through to step 3.

### `.env` load order — `load_settings()`

Always before any `os.environ` read, both with `override=False` (a truly exported env
var always wins; between the two `.env` files, the first one loaded wins):

1. `load_dotenv(<corpus_root>/.env, override=False)`.
2. `find_dotenv(usecwd=True)` (nearest `.env` walking up from `cwd`) → if found,
   `load_dotenv(that, override=False)`.

### Path overrides — `_env_path(name, default)`

`PAPERTRAIL_<NAME>` wins over the bare legacy `<NAME>` (kept for one release), which
wins over the default. Values are resolved to absolute paths.

| Setting | Env pair | Default |
|---|---|---|
| `data_dir` | `PAPERTRAIL_DATA_DIR` / `DATA_DIR` | `<corpus_root>/data` |
| `sections_dir` | `PAPERTRAIL_SECTIONS_DIR` / `SECTIONS_DIR` | `<data_dir>/sections` |
| `assets_dir` | `PAPERTRAIL_ASSETS_DIR` / `ASSETS_DIR` | `<data_dir>/assets` |
| `classification_dir` | `PAPERTRAIL_CLASSIFICATION_DIR` / `CLASSIFICATION_DIR` | `<data_dir>/classification` |
| `wiki_dir` | `PAPERTRAIL_WIKI_DIR` / `WIKI_DIR` | `<corpus_root>/wiki` |
| `sqlite_path` | `PAPERTRAIL_KB_SQLITE` / `KB_SQLITE` | `<data_dir>/kb.sqlite` |

Note `wiki_dir` hangs off `corpus_root` directly, not `data_dir` — overriding
`DATA_DIR` moves sections/assets/classification/sqlite together but leaves `wiki_dir`
where it was.

### LLM / model variables

Plain `os.getenv(name, default)` — no `PAPERTRAIL_` alias for these:

| Env var | Default |
|---|---|
| `BASE_URL` | `""` |
| `API_KEY` | `""` |
| `BULK_MODEL` | `"deepseek/deepseek-v4-flash-0731"` |
| `SYNTH_MODEL` | `"anthropic/claude-opus-4.8"` |
| `CHAT_MODEL` | `"anthropic/claude-sonnet-5"` |
| `PIPELINE_BACKEND` | `"openai"` (`"codex"` is also supported) |
| `PIPELINE_CODEX_MODEL` | `CODEX_MODEL`, then the Codex account default |
| `AGENT_BACKEND` | `"langchain"` (`"codex"` is also supported) |
| `AGENT_MODEL` | `CHAT_MODEL` |
| `CODEX_MODEL` | `""` (use the Codex account/config default) |
| `CODEX_BIN` | `""` (find `codex` on `PATH`, then fall back to the SDK runtime) |
| `AGENT_MCP_URL` | `"http://127.0.0.1:8013/mcp"` |
| `AGENT_TIMEOUT_SECONDS` | `"120"` |
| `AGENT_RECURSION_LIMIT` | `"30"` |

The three model defaults are literal gateway-specific model identifiers — they will 404
against a stock OpenAI-compatible endpoint that doesn't recognize them. Set all three
explicitly for your own gateway.

`PIPELINE_BACKEND` selects the provider for Stages B–D together. `openai` requires
`BASE_URL`/`API_KEY` and uses `BULK_MODEL` for B/C plus `SYNTH_MODEL` for D. `codex`
reuses `codex login`, requires no `API_KEY`, and resolves its model from
`PIPELINE_CODEX_MODEL`, then `CODEX_MODEL`, then the account default. Codex defaults to
2 workers in B/C; the OpenAI-compatible defaults remain 8 and 5 respectively, and an
explicit `--workers` overrides either default.

`BASE_URL`, `API_KEY`, and `AGENT_MODEL` configure the `langchain` browser-chat
backend. The `codex` backend instead reuses the official CLI/SDK login established by
`codex login`; it does not consume `API_KEY`. `CODEX_MODEL` is optional.

### Other env vars, read elsewhere in the package

| Env var | Where | Default |
|---|---|---|
| `ROOT_PATH` | `web/asgi.py` | `""` |
| `MCP_TRANSPORT` | `mcp/server.py` | `"stdio"` (`"http"` / `"streamable-http"` → Streamable HTTP) |
| `MCP_HOST` | `mcp/server.py` | `"127.0.0.1"` |
| `MCP_PORT` | `mcp/server.py` | `"8000"` |
| `WEBAPP_BASE_URL` | `mcp/server.py` (`publish_answer`'s returned URL) | `"http://127.0.0.1:8000"` |
| `WEBAPP_TEST_PORT` | `tests/test_live_smoke.py` | `"8009"` |

### `Settings` / `Corpus`

`Settings` (frozen dataclass): `corpus_root, data_dir, sections_dir, assets_dir,
classification_dir, wiki_dir, sqlite_path, base_url, api_key, bulk_model, synth_model,
chat_model, pipeline_backend, pipeline_codex_model, agent_backend, agent_model,
agent_mcp_url, agent_timeout_seconds, agent_recursion_limit, codex_model, codex_bin,
corpus`.

`Corpus` (frozen dataclass): `root`, `raw` (the full parsed `corpus.yaml`). Properties:
`name`, `title`, `glyph`, `description`, `taxonomy`, `navigation` (§3). Methods:
`marker_dir()`, `load_module(mod_path)`, `adapter_module()`, `load_papers()` (§4).

`Corpus.load_module(mod_path)` is the one entry-point resolver shared by
`source.metadata_adapter` and `identity.citation_style`:
- `mod_path` starting with `.` or `/` → a file path, corpus-relative if not absolute,
  loaded via `importlib.util.spec_from_file_location` under a unique module name. No
  `sys.path` mutation, no import-name collisions.
- Anything else → a dotted module path, loaded via `importlib.import_module` after
  inserting the corpus root's **parent** directory onto `sys.path`.

---

## 3. `corpus.yaml` schema

Two live examples: `corpora/wasp12b/corpus.yaml` (maximal — 4 facets, custom
`CitationStyle`, external links) and `corpora/demo/corpus.yaml` (minimal/different — 2
facets, `plain` style, no external links). Validation: `src/papertrail/taxonomy.py`
(`load_taxonomy`/`load_navigation`) and `src/papertrail/identity.py`
(`load_citation_style`).

### `corpus:` — optional block, every key optional

| Key | Read by | Default if omitted |
|---|---|---|
| `name` | `Corpus.name` | the corpus root directory's basename |
| `title` | `Corpus.title` | `"{name} Knowledge Base"` |
| `description` | `Corpus.description` | `""` |
| `glyph` | `Corpus.glyph` | `""` |
| `subject` | `prompts.base_vars` → `$subject` | `"the documents in this corpus"` |
| `citation_example.bad` | `prompts.base_vars` → `$citation_example_bad` | `""` |
| `citation_example.good` | `prompts.base_vars` → `$citation_example_good` | `""` |

`name`/`title`/`description`/`glyph` are served by `GET /api/corpus` for SPA branding.
`subject`/`citation_example` are interpolated into every LLM system prompt (§ prompts,
in USERS_GUIDE.md).

### `source:` — required

| Key | Required? | Notes |
|---|---|---|
| `marker_dir` | **Yes.** `Corpus.marker_dir()` does `raw["source"]["marker_dir"]` — `KeyError` if absent. Corpus-relative unless absolute. | An adapter that resolves its own marker path (like `corpora/demo/adapter.py`) still must set this key, even if nothing reads its value in that case. |
| `metadata_adapter` | Required unless `documents_file` is set. `"<mod_path>:<func_name>"`, split on `:`. | Resolved via `Corpus.load_module` (§2). Mutually exclusive with `documents_file`. |
| `documents_file` | Required unless `metadata_adapter` is set. | Corpus-relative YAML containing a top-level `documents:` list. Intended for small or directly ingested corpora. |
| `doc_id_field` | **No — dead key.** Zero readers anywhere in `src/papertrail`. | Both shipped corpora set it anyway (`safe_bibcode`, `doc_id`) with a comment saying it's informational only — don't assume it does anything. |

### `identity:` — fully optional

| Key | Default | Behavior |
|---|---|---|
| `citation_style` | `"author_year"` | Builtins: `author_year`, `plain`. Anything else is parsed as `"<mod_path>:ClassName"` and loaded via `Corpus.load_module`. |
| `external_links` | `[]` | List of `{label, url}`; `url` is a template string (§4). |

**Omitting `identity:` entirely is valid** — you get `AuthorYearStyle` with no link
specs: `label` falls back to `doc_id`, `reference_label` to a 40-char title snippet, and
no external links are ever rendered.

- **`AuthorYearStyle`** (default): `label` = first 4 chars of `doc_id` if numeric, else
  `str(year)`, else `doc_id`. `reference_label` = `"Lastname YYYY"` / `"A & B YYYY"` /
  `"A et al. YYYY"`, parsed from a `"Last, First; Last, First"` string or a list.
  `mention_pattern()` returns the author-year regex (calibrated for the uncited-mention
  heuristic — see USERS_GUIDE.md), `mention_window()` = `200`.
- **`PlainStyle`**: `label` = `doc_id`, `reference_label` = `title[:40]` or `doc_id`,
  `mention_pattern()` returns **`None`** — disables the uncited-mention heuristic
  entirely, for a corpus with no author-year convention worth enforcing.
- **Custom style**: e.g. `corpora/wasp12b/style.py`'s `Wasp12bStyle`, a 6-line
  `AuthorYearStyle` subclass overriding only `canonicalize()` (to also try a `&`↔`+`
  filesystem-escape alias when looking up a doc_id).

**External-link template rules:** available variables are `doc_id`, `native_id`, `year`,
`title`, `journal`, plus every key in the record's `extra` dict. Values are
percent-encoded by default (safe: `-._~:`); `{var:raw}` passes a value through verbatim
(used for DOIs, so their `/` isn't escaped). **A link whose template references any
missing or empty variable is silently dropped** — checked by inspecting which fields the
template references (`string.Formatter().parse()`), not by pattern-matching the rendered
URL. This is the entire "no bibcode → no ADS link" mechanism; there is no separate
`when:` condition to write.

### `taxonomy:` — a mapping of facet name → spec

Every sub-key is optional; only the facet name itself is required.

| Sub-key | Default | Meaning |
|---|---|---|
| `values` | `[]` | Closed vocabulary shown to the classifier LLM, and used to build MCP enum params |
| `multi` | `false` | Whether more than one value is allowed per doc |
| `label` | `name.replace("_", " ").title()` | Display name (web `/api/corpus`, MCP table headers) |
| `topic_pages` | **`true`** | Whether this facet gets Stage D cross-paper topic pages |
| `slug_prefix` | the facet name | Topic-page filename prefix: `<slug_prefix>-<slugified value>.md` |
| `similarity_weight` | `1` | Weight in Stage C's related-paper scoring; `0` excludes the facet entirely |
| `description` | `""` | Not consumed anywhere downstream — informational only |

**Eager validation at load time:** duplicate `slug_prefix` among `topic_pages: true`
facets raises `TaxonomyConfigError`. No fixed facet count — wasp12b has 4, demo has 2.

`classify.py` reads `settings.corpus.raw["taxonomy"]` directly — **`taxonomy:` is
required for Stage B** (`KeyError` if absent). An empty `taxonomy: {}` mapping is
tolerated by `load_taxonomy` itself, but Stage B will then have nothing to classify.

### `navigation:` — optional block

| Key | Default when omitted | Validation |
|---|---|---|
| `tree_facet` | first declared taxonomy facet (insertion order); `""` if no taxonomy | must name a declared facet, else `TaxonomyConfigError` |
| `table_columns` | `[]` | every entry must name a declared facet, else `TaxonomyConfigError` listing the unknowns |
| `year_pages` | `true` | — |
| `digest_headings` | first entry of `wiki_headings` | every entry must be a name present in `wiki_headings`, else `TaxonomyConfigError` |

`year_pages: false` is a supported, tested combination (`corpora/demo/corpus.yaml` sets
it, since its adapter never supplies a `year`).

The `digest_headings` check is the motivating example for eager validation: renaming a
wiki heading without updating the digest list used to silently break every topic page
with a misleading "no member paper has a wiki page yet" error.

### `wiki_headings:` — a list; required for Stage C

`paper_pages.py` reads `settings.corpus.raw["wiki_headings"]` directly — `KeyError` if
absent.

Each entry is either a bare string, or `{name, may_not_apply, guidance}`. A bare string
is normalized to `{name: X, may_not_apply: false, guidance: ""}`.

- `may_not_apply: true` → the heading's name is woven into the paper-page prompt's
  "if a section genuinely doesn't apply, say so honestly" clause.
- **`guidance` is parsed but never interpolated into any prompt.** Both shipped
  `corpus.yaml`s set a `guidance` string; it currently does nothing. Reserved for future
  use, not a bug to work around — just don't expect it to have an effect today.

wasp12b declares 6 headings, demo declares 3 — nothing assumes a fixed count.

### `models:` — dead block

Both shipped corpora declare `models: {bulk_env: BULK_MODEL, synth_env: SYNTH_MODEL,
chat_env: CHAT_MODEL}`, but there are zero readers of `models`/`bulk_env`/`synth_env`/
`chat_env` anywhere in `src/papertrail`. Model selection comes only from the
`BULK_MODEL`/`SYNTH_MODEL`/`CHAT_MODEL` **environment variables** (§2). Treat this block
as accepted-but-currently-ignored, not a way to rename the env vars per corpus.

### `prompts_dir:` — optional, top-level (not nested)

Corpus-relative directory. If `<prompts_dir>/<template_name>.txt` exists, it replaces
the packaged default **wholesale** — no inheritance, no partial overrides within one
template. Neither shipped corpus sets this. See USERS_GUIDE.md's prompts section for the
six template names and which `$variables` each expects.

---

## 4. Adapter & `CitationStyle` interfaces

### Metadata adapter contract

Referenced from `corpus.yaml` as `source.metadata_adapter: "<mod_path>:<func_name>"`.
The function takes **no arguments** and returns `list[dict]`.

| Key | Required? | Consumed by |
|---|---|---|
| `doc_id` | **Yes** | Every stage keys on it. Must match the Marker JSON filename stem: `<marker_dir>/<doc_id>.json`. |
| `title` | Yes in practice | Stage E's `docs` insert, the classify prompt, wiki frontmatter. Stage A doesn't need it. |
| `authors` | Optional, `str` or `list[str]` | `AuthorYearStyle.reference_label` (handles both a `"Last, First; Last, First"` string and a list); json-dumped into `docs.authors_json`. |
| `year` | Optional, `int \| None` | Year topic pages (skipped if falsy), `docs.year`, MCP `list_papers(year=...)`. |
| `journal` | Optional | `docs.journal`, the doc header in `GET /api/documents/{doc_id}`. |
| `extra` | Optional `dict` | Merged into the external-link template namespace (§3); stored as `docs.extra_json`. |
| `native_id` | Optional | The pre-escaping identifier for external-link templates (`{native_id}`); stored as `docs.native_id`. Omit entirely for a corpus with no such identifier. |
| `has_fulltext` | Optional, three-valued `bool \| None` | **Load-bearing.** Stage A skips a record only if this is explicitly `False`; `None` (a minimal adapter) falls through to a marker-file-existence check instead. Stage E uses it to decide a full node tree vs. a single stub node. |
| `fulltext_source` | Optional | Stage E does `paper.get("fulltext_source") or "publisher_pdf"`. |
| `skip_reason` | Optional, **unread** | wasp12b's adapter sets it; nothing in `src/papertrail` reads it. |

**Two reference adapters:**
- `corpora/wasp12b/adapter.py` — wraps an existing loader (`paper_download/config.py`),
  computing `has_fulltext`/`fulltext_source` from a PDF-fetch manifest. Loaded via
  `importlib.util.spec_from_file_location` under a unique module name.
- `corpora/demo/adapter.py` — the "no metadata file at all" tier: a fixed doc_id list,
  `title` derived by scanning the Marker JSON for its first `SectionHeader` block's text,
  `authors`/`year`/`journal`/`native_id` all absent, `has_fulltext: True` /
  `fulltext_source: "publisher_pdf"` unconditionally.

### `CitationStyle` protocol

`src/papertrail/identity.py`:

```python
class CitationStyle(Protocol):
    def label(self, ref: DocRef) -> str: ...
    def reference_label(self, ref: DocRef) -> str: ...
    def external_links(self, ref: DocRef) -> list[ExternalLink]: ...
    def canonicalize(self, raw: str) -> list[str]: ...       # candidate doc_ids, in order
    def mention_pattern(self) -> re.Pattern | None: ...        # None disables the heuristic
    def mention_window(self) -> int: ...
```

`DocRef` (frozen dataclass): `doc_id: str`, `native_id: str | None = None`, `title: str
= ""`, `authors: str | list[str] = ""`, `year: int | None = None`, `journal: str = ""`,
`extra: dict = {}` — a superset of the adapter record shape and the `docs` table row, so
both construct one consistently.

| | `AuthorYearStyle` (default) | `PlainStyle` |
|---|---|---|
| `label` | first 4 digits of `doc_id`, or `year`, or `doc_id` | `doc_id` |
| `reference_label` | `"Lastname YYYY"` / `"A & B YYYY"` / `"A et al. YYYY"` | `title[:40]` or `doc_id` |
| `mention_pattern()` | author-year regex | `None` (heuristic off) |
| `mention_window()` | `200` | `200` (unused, since pattern is `None`) |

---

## 5. On-disk artifact layout

```
<corpus_root>/
  corpus.yaml
  adapter.py            (or wherever source.metadata_adapter points)
  style.py               (optional, custom CitationStyle)
  prompts/                (optional, prompts_dir override)
  .env                    (optional, corpus-specific credentials/paths)
  data/
    sections/<doc_id>.json          Stage A — atomic write (.tmp + os.replace)
    assets/<doc_id>/<block>.jpeg    Stage A — figure images
    classification/<doc_id>.yaml    Stage B
    kb.sqlite                        Stage E
  wiki/
    papers/<doc_id>.md               Stage C, atomic write
    papers/<doc_id>.citations.json   Stage C, the [n] -> (doc_id, node_id) map
    topics/<kind>-<slug>.md          Stage D
    topics/<kind>-<slug>.citations.json
    index.md                         Stage D
    index.citations.json
```

`data/sections/`, `data/classification/`, and `wiki/*.md`/`*.citations.json` are all
regenerated by re-running their producing stage — durable only in the sense that nobody
deletes them automatically. `data/kb.sqlite`'s **content tables** (`docs`, `doc_facets`,
`nodes`, `chunks_fts`) are fully dropped and rebuilt by every Stage E run; its
**persistent tables** (`conversations`, `answers`, `messages`) are created once and never
dropped by the pipeline (§8) — a Stage E re-run does not erase chat history or published
answers.

---

## 6. HTTP API

Source: `src/papertrail/web/app.py` (`create_app`). All routes are defined inside the
`create_app(settings, root_path)` factory — nothing is registered at import time.

| Method | Path | Request | Response |
|---|---|---|---|
| GET | `/api/health` | — | `{status: "ok", corpus: <name>, doc_count: int, agent: {backend, status, model, auth}}`; agent status is `not_checked`, `ready`, `unavailable`, or `error` |
| GET | `/api/corpus` | — | `{name, title, description, glyph, facets: {<facet>: {label, values, multi}}, navigation: {tree_facet, table_columns}}` |
| POST | `/api/reload` | — | `{status: "ok", doc_count: int}` — drops registry caches and re-reads `kb.sqlite`/`corpus.yaml`; no process restart needed after a pipeline run |
| GET | `/api/documents` | — | the left-nav doc tree, grouped by `navigation.tree_facet` |
| GET | `/api/documents/{doc_id}` | — | `{doc_id, doc_name, label, has_fulltext, tree, html, reference_label, journal, external_links: [{label,url}]}`. If `has_fulltext` is false: `tree: []`, `html: ""`. 404 `{"detail": "unknown doc_id: X"}` |
| GET | `/api/documents/{doc_id}/sections/{node_id}` | — | `{node_id, title, block_type, text, children}`. 404 on unknown `doc_id` or `node_id` |
| GET | `/api/search` | `q` (required, min length 1), `limit` (default 20) | `{query, hits: [{doc_id, node_id, block_type, title, year, snippet}]}` |
| GET | `/api/wiki/{page:path}` | — | `{page, markdown}`. `.md` suffix stripped; path-traversal-guarded. 404 detail is a dict: `{"error": "page not found: X", "available": [...]}` |
| POST | `/api/conversations` | `{name?}` | `{id, name}` (`name` defaults to `"New conversation"`) |
| GET | `/api/conversations` | — | list of conversations |
| GET | `/api/conversations/{id}/messages` | — | list of `{role, text, payload, ...}` |
| PATCH | `/api/conversations/{id}` | `{name}` | `{status: "ok"}` |
| DELETE | `/api/conversations/{id}` | — | `{status: "ok"}` |
| POST | `/api/chat` | `{conversation_id, question}` | **SSE stream** (`text/event-stream`); 400 if either field is missing. Grammar below. |
| POST | `/api/answers` | an answer payload + optional `title`; query `?strict=false` (default) | 200 `{id, url: "/#answer=<id>", **enriched}`; 400 on invalid JSON/`ValueError`; **422** `{detail: "invalid citations", offending: [...]}` on unresolved citations in strict mode |
| GET | `/api/answers/{id}` | — | the stored answer; 404 `{"detail": "unknown answer id: X"}` |
| GET | `/api/answers` | `kind` (optional) | list, optionally filtered |
| GET | `/` | — | the packaged SPA (`index.html`, read once at factory time) |
| — | `/assets` | — | `StaticFiles` mount, **only if `settings.assets_dir.is_dir()` at factory time** — a dir created after startup needs a process restart |

`root_path` (constructor arg to `create_app`, from `ROOT_PATH` in `web/asgi.py`) is
threaded into every rendered `<img src="{root_path}/assets/...">` — this fixes URLs
baked into HTML, which uvicorn's own `--root-path` (routing/OpenAPI only) does not.

### `/api/chat` SSE grammar

Source: `src/papertrail/web/agent_chat.py` (`AgentChatService.stream_chat`). Each frame is
`data: <json>\n\n`. Three kinds of event:

- **Step**: `{"event": "step", "step": <human-readable name>, "status":
  "start"|"done"}`. The stream names connection, planning/correction, each MCP tool
  call, and citation validation without exposing hidden reasoning or raw tool payloads.
- **Terminal** (always exactly one, last frame): `{"event": "done", "answer_id": <id>,
  **enriched_payload}` — the enriched payload shape is §9's output shape.
- **Error**: `{"event": "error", "code": <stable code>, "message": <safe message>}`.
  Errors are not persisted. Codes currently include `agent_unavailable`,
  `agent_timeout`, `research_limit`, `invalid_agent_output`, and `agent_failed`.

The agent gets at most one correction attempt. The backend requires every cited node to
have been successfully read with `get_section` during that attempt, requires exact
marker/citation-id agreement, then runs `validate_and_enrich(strict=True)` before any
answer or message is stored.

---

## 7. MCP tools

Source: `src/papertrail/mcp/server.py` (`build_server(settings)`). Every tool is defined
*inside* the factory, closing over `settings` — required so a `settings` parameter never
leaks into a tool's JSON schema, and so every tool in one server instance shares one
corpus. Server name: `f"{settings.corpus.name}-kb"`.

| # | Tool | Signature | Description |
|---|---|---|---|
| 1 | `search_fulltext` | `(query: str, limit: int = 10, *, <one kwarg per taxonomy facet>) -> str` — **dynamic** | FTS5/BM25 over every paragraph/table/figure-caption/equation/reference node. Hits carry the exact `(doc_id, node_id)`. |
| 2 | `get_paper` | `(doc_id: str) -> str` | YAML metadata block + the generated wiki page. Resolves `doc_id` through `style.canonicalize()` alternates first. Metadata-only docs get an explicit "nothing beyond title/authors/year is citable" note. |
| 3 | `get_section` | `(doc_id: str, node_id: str, context: int = 0) -> str` | One node: the whole subtree if it's a heading, else the leaf prefixed with its parent heading's title. `context=N` widens by `N` siblings **within the same section only**. Figure nodes get a pointer to `get_figure_image`. |
| 4 | `get_fulltext` | `(doc_id: str) -> str` | Progressive disclosure: the whole flattened doc if ≤20,000 chars, else a heading index (with `node_id`s) plus the first 8,000 chars. |
| 5 | `read_wiki` | `(page: str = "index") -> str` | Any wiki page by relative name (`"index"`, `"topics/theme-orbital-decay"`, `"papers/<doc_id>"`). On a miss, returns the full list of available page names. |
| 6 | `list_papers` | `(year: int \| None = None, *, <one kwarg per taxonomy facet>) -> str` — **dynamic** | Markdown table of matching papers. **Called with no arguments**, returns corpus coverage (total / full-text / metadata-only counts) plus every distinct facet value and year — the discovery-by-empty-call idiom. |
| 7 | `get_figure_image` | `(doc_id: str, node_id: str) -> Image \| str` | The actual JPEG bytes for a figure node; an error string if the node isn't a figure, has no image asset, or the file is missing on disk. |
| 8 | `validate_answer` | `(payload: dict) -> dict` | Strict citation-resolution check. Returns the enriched payload, or `{error, offending}`. Proves a cited node *exists* — not that it supports the claim beside it. |
| 9 | `publish_answer` | `(payload: dict, title: str \| None = None) -> dict` | Validates (strict) then stores as `kind='agent_adhoc'`. Returns `{id, url}` (`url` = `f"{WEBAPP_BASE_URL}/#answer={id}"`), or `{error, offending}`. |

### The dynamic-signature mechanism (tools 1 and 6)

1. `_facet_params(taxonomy)` builds one keyword-only `inspect.Parameter` per declared
   facet, default `None`. If the facet has ≤ `MAX_ENUM_VALUES` (`30`) values, its
   annotation is `Annotated[Literal[tuple(values)] | None, Field(description=...)]` — a
   real JSON-Schema `enum`. Above that, it degrades to `Annotated[str | None, ...]` with
   the vocabulary spelled out in the description text instead (an enum with hundreds of
   values makes for an unreadable tool schema).
2. `_bind_signature(fn, params, ...)` assigns `fn.__signature__`/`__annotations__`/
   `__doc__` at server-build time, from the corpus's actual taxonomy.
3. Registration uses `mcp.add_tool(fn, name=..., description=...)` rather than the
   `@mcp.tool()` decorator — the decorator runs at function-definition time, before the
   signature can be rewritten.

So for wasp12b, `search_fulltext`/`list_papers` get `research_theme`/`method`/
`facility`/`role_of_wasp12b` enum params; for the demo corpus they get `method`/
`outcome`. Zero code change either way. An out-of-vocabulary filter value is rejected as
a tool error (surfaced via pydantic validation), not silently ignored.

### Transport

`MCP_TRANSPORT` default `"stdio"`. `"http"`/`"streamable-http"` → Streamable HTTP,
stateless (`MCP_HOST` default `127.0.0.1`, `MCP_PORT` default `8000`). The equivalent
CLI flags take precedence. Browser chat conventionally uses port `8013`.

`.mcp.json` shape:
```json
{"mcpServers": {"<name>": {"command": ".venv/bin/papertrail", "args": ["mcp", "--corpus", "corpora/wasp12b"]}}}
```
(`PAPERTRAIL_CORPUS` in `env` works too, and is what pre-dates the `--corpus` flag being
wired up — either mechanism resolves the same way, §2.)

---

## 8. SQLite schema

Source: `src/papertrail/core/schema.py`.

### Content tables — dropped and rebuilt every Stage E run

```sql
CREATE TABLE docs (
    doc_id TEXT PRIMARY KEY,
    title TEXT, year INTEGER, journal TEXT, authors_json TEXT,
    fulltext_source TEXT,
    has_fulltext INTEGER NOT NULL,
    facets_json TEXT NOT NULL DEFAULT '{}',
    native_id TEXT,
    extra_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE doc_facets (
    doc_id TEXT NOT NULL REFERENCES docs(doc_id),
    facet TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (doc_id, facet, value)
);
CREATE INDEX ix_doc_facets_fv ON doc_facets(facet, value);

CREATE TABLE nodes (
    id INTEGER PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES docs(doc_id),
    node_id TEXT NOT NULL,
    kind TEXT NOT NULL,             -- container | leaf
    block_type TEXT,                 -- paragraph|equation|table|figure|footnote|reference|stub|NULL(container)
    parent_title TEXT,
    page INTEGER,
    text TEXT NOT NULL,
    UNIQUE (doc_id, node_id)
);

CREATE VIRTUAL TABLE chunks_fts USING fts5(
    text, content='nodes', content_rowid='id', tokenize='porter unicode61'
);
```

`facets_json` is a display blob (`{facet: value_or_list}`, one dict per doc) —
`doc_facets` is the same data as queryable rows, including exactly one row for a
single-valued (`multi: false`) facet. Both are kept in sync by Stage E; nothing else
writes either.

### Persistent tables — created once (`CREATE TABLE IF NOT EXISTS`), never dropped by the pipeline

```sql
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS answers (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,              -- chat_message | wiki_paper | wiki_topic | agent_adhoc
    conversation_id INTEGER REFERENCES conversations(id),
    title TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL,              -- user | assistant
    text TEXT,
    answer_id TEXT REFERENCES answers(id),
    created_at TEXT NOT NULL
);
```

A Stage E rebuild calls `ensure_persistent()` (idempotent, additive-only) then
`rebuild_content()` (drops and recreates the content tables listed above) — chat history
and published answers survive every re-index.

---

## 9. Answer payload schema

Source: `src/papertrail/core/citations.py` (`validate_and_enrich`).

### Input (what a producer supplies)

```json
{"results": [
  {"question": "...", "answer": "prose with [1] and [2] markers...",
   "citations": [{"id": 1, "doc_id": "...", "node_id": "..."},
                 {"id": 2, "doc_id": "...", "node_id": "..."}]}
]}
```
A single bare result object (no `results` wrapper, `{"answer": ..., "citations": [...]}`)
is also accepted and normalized to the same shape.

### Output (the enriched payload — what gets stored and rendered)

```json
{"results": [
  {"label": "...", "question": "...", "answer": "...",
   "citations": [
     {"id": 1, "doc_id": "...", "node_id": "...", "doc_name": "...",
      "section_title": "...", "block_type": "paragraph", "page": 6,
      "reference_label": "Lastname et al. 2011", "journal": "...",
      "external_links": [{"label": "ADS", "url": "https://..."}]}
   ]}],
 "documents": ["doc_id", "..."],
 "warnings": ["..."]}
```

**Field rename, relevant to anything reading old stored payloads:** `reference_label` +
`external_links` replaced the pre-Step-9 `author_year` + `ads_url` fields. The packaged
SPA's frontend reads either shape (a backward-compat shim over already-stored rows).

### Validation rules

1. Every `citations[].{doc_id, node_id}` pair must resolve to a real node, **and** the
   cited `doc_id` must have `has_fulltext = 1` — citing a metadata-only stub doc is
   always an error (nothing to cite).
2. **Non-fatal by default** (`strict=False`): unresolved citations become entries in
   `warnings[]` and are dropped from the enriched output rather than failing the whole
   request. `strict=True` (used by MCP's `validate_answer`/`publish_answer`) instead
   raises `InvalidCitationsError`, whose `.offending` list has the shape
   `{result_index, id, doc_id, node_id, reason}`.
3. Two more warning-only checks, independent of citation resolution: an `[n]` marker
   used in the prose with no matching citation entry, or a citation entry whose `id`
   never appears as an `[n]` marker.
4. `find_uncited_author_year_mentions(answer, style)` — an author-year mention (e.g.
   `"Madhusudhan et al. (2011)"`) with no `[n]` marker within `style.mention_window()`
   chars after it. Style-dependent: `PlainStyle.mention_pattern()` returns `None`, making
   this a no-op for a corpus using it.

---

## 10. Tunable constants

Hardcoded in source — no config knob exists for any of these. Listed here so a
correctness question ("why 200 chars, why 3 sibling nodes") has one place to look.

| Constant | Value | Where | What it bounds |
|---|---|---|---|
| `MAX_BODY_WORDS` | `4000` | `pipeline/classify.py` | Words of body text shown to the classifier LLM per paper |
| `RETRIES` (classify) | `2` | `pipeline/classify.py` | Retry attempts on a malformed/out-of-vocabulary classification response |
| `RETRIES` (paper-pages) | `3` | `pipeline/paper_pages.py` | Retry attempts per heading (4 total attempts) — citation-heavy papers need more |
| `RETRIES` (topic-pages) | `3` | `pipeline/topic_pages.py` | Same, for topic synthesis |
| `MAX_REFERENCE_NODES_SHOWN` | `40` | `pipeline/paper_pages.py` | Cap on bibliography entries included in a paper's citation-candidate pool |
| `TABLE_TEXT_PREVIEW_CHARS` | `400` | `pipeline/paper_pages.py` | Truncation of raw `<table>` HTML in the citation-candidate pool |
| `MAX_RELATED` | `5` | `pipeline/paper_pages.py` | Max entries in a paper page's `related_papers` list |
| `MAX_ENUM_VALUES` | `30` | `mcp/server.py` | Above this many facet values, an MCP tool param degrades from a JSON-Schema enum to a free string |
| author-year `mention_window` | `200` chars | `identity.py` (`AuthorYearStyle`) | Window after an author-year mention to look for an `[n]` marker (empirically calibrated on wasp12b: legitimate citations observed 100–345 chars downstream) |
| `get_fulltext` inline threshold | `20,000` chars | `mcp/server.py` | Below this, `get_fulltext` returns the whole doc; above it, a heading index + preview |
| `get_fulltext` preview length | `8,000` chars | `mcp/server.py` | Preview shown when a doc exceeds the inline threshold |
| agent history | last `12` stored messages | `web/agent_chat.py` | User and final assistant text included as conversational context |
| agent correction attempts | `1` | `web/agent_chat.py` | One fresh research attempt after a malformed, unread, or invalid citation payload |
