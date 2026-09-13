# Developers Guide

For someone changing `papertrail` itself, not just configuring a corpus. If you're
adopting papertrail for a corpus, see [USERS_GUIDE.md](USERS_GUIDE.md) instead — nothing
here is required reading for that.

## 1. Module map

```
src/papertrail/
  config.py      Settings/Corpus — corpus.yaml resolution, env overrides         \
  identity.py    CitationStyle protocol + AuthorYearStyle/PlainStyle              | leaf modules —
  taxonomy.py    FacetSpec/Navigation — taxonomy + doc-tree/table config          | import nothing
  prompts.py     string.Template rendering, prompts_dir override, versioning     | else in the package
  textutils.py   slugify                                                        /
  core/          FastAPI-free — imports only the leaf modules above
    registry.py    kb.sqlite-driven doc tree + lazy per-doc HTML rendering
    renderer.py     section-tree -> HTML (markdown library + custom node handling)
    nodes.py        pure tree walkers (iter_nodes, context_window, find_node_with_siblings, ...)
    citations.py    validate_and_enrich — the one shared citation validator
    store.py        conversations/answers/messages CRUD
    search.py       FTS5/BM25 (search_nodes for chat, run_search/paper_filters_sql for MCP)
    schema.py       sole owner of all SQLite DDL
  pipeline/       imports core + leaf modules; never web/ or mcp/
    llm.py           provider-neutral OpenAI-compatible/Codex generation seam
    sections.py      Stage A
    classify.py      Stage B
    paper_pages.py   Stage C
    topic_pages.py   Stage D
    index.py         Stage E
  web/            imports core + leaf modules; never pipeline/ or mcp/
    app.py           create_app(settings, root_path) factory — all routes
    agent_chat.py     AgentChatService — MCP tool loop, grounding, SSE, persistence
    asgi.py            module-level `app = create_app(...)`, the one sanctioned exception
                       to "nothing at import time" (see §2)
    static/index.html  packaged single-file SPA
  mcp/            imports core + leaf modules; never pipeline/ or web/
    server.py        build_server(settings) factory — all 9 tools, closures
  cli.py          bare dispatch table, `papertrail <command>` -> module.main(argv)
```

**The layering rule that does all the work: `core/` never imports `pipeline/`, `web/`,
or `mcp/`; those three are siblings that never import each other.** Before the
extraction, this didn't hold — `pipeline/` code reached into what's now `web/`'s
territory via `sys.path` hacks, and the MCP server injected a top-level package literally
named `app` to reach the web app's modules. `core/nodes.py` exists as its own file (split
out of `core/registry.py`) specifically so `core/renderer.py` can import the tree walkers
without a circular dependency on `core/registry.py` — before the split,
`core/renderer.py` did a function-local import of `registry.py` just to dodge that cycle.

## 2. The four governing invariants

Each of these is enforced by convention, not by a lint rule — know them before changing
anything that touches config, the section-tree model, citations, or multi-corpus
serving.

**(a) No import-time side effects.** Nothing in `papertrail` computes a path from
`__file__`, creates a directory, or constructs a network client at import time. Every
stage's `main()` calls `load_settings(args.corpus, create_dirs=True)` explicitly; the web
app and MCP server call it with `create_dirs` left `False`. The **one sanctioned
exception** is `web/asgi.py`'s module-level `app = create_app(root_path=os.getenv(...))`
— justified there because that's exactly what an ASGI server needs to find `app`, and
`load_settings()` itself has no side effects (it only resolves paths and reads `.env`).
*Historical bug this prevents:* the pre-extraction `kb/config.py` hardcoded every path
`Path(__file__).parent`-relative and `mkdir`'d seven directories on import — installed as
a package, that would have written into `site-packages`. A second, related bug: the
pre-extraction `webapp/app/config.py` called `load_dotenv()` **after** already reading
`os.environ` for its own path overrides, so `.env`-only path variables were silently
ignored. `config.py`'s current `load_settings()` makes both bugs structurally impossible
by being one function body with a fixed, explicit order.

**(b) Parent/child text is disjoint in the section tree.** A container node holds only
its own heading's text; concatenating every node in document order reconstructs the
source document exactly once, with no duplication. Every citation, every rendering path,
and Stage A's own "did I already build this" resume check all depend on this holding. If
you touch `pipeline/sections.py`'s tree-assembly algorithm, verify this invariant first
(the golden tests in `tests/test_build_sections.py` check exact output, which is a proxy
for it, but the invariant itself is the thing to protect).

**(c) One validator for every citable producer.** Chat, Stage C paper pages, Stage D
topic pages, and MCP's `publish_answer` all bottom out in `core/citations.py`'s
`validate_and_enrich()` — not four similar implementations. If you add a fifth producer
of citable content, it must call this function, not reimplement citation checking.

**(d) `Settings` is passed explicitly, never read from a module global.** Every function
that needs corpus configuration takes a `settings` parameter; nothing does
`from papertrail.config import SOME_GLOBAL`. This is what makes one process able to serve
two different corpora (e.g., two MCP server instances, or tests constructing a scratch
`Settings` without touching any real corpus). *Historical bug this prevents:* the
pre-extraction `kb/mcp_server.py` had a real split-brain — its search tools imported
`kb.config` (hardcoded paths, no env override) while its authoring tools went through
`webapp.app.config` (env-override-aware) via `CorpusRegistry`. Setting `KB_SQLITE` made
search tools and `validate_answer`/`publish_answer` silently see two different corpora in
the same process. `mcp/server.py`'s `build_server(settings)` — every tool defined as a
closure over one shared `settings` — makes this class of bug impossible: there is
exactly one `Settings` object per server instance, and every tool reads it.

**A fifth, narrower thing to know**, caught for real rather than prevented in advance:
`pipeline/index.py` had a hardcoded `FACET_NAMES = ("research_theme", "method",
"facility", "role_of_wasp12b")` tuple that silently dropped any taxonomy facet not in
that literal list — a latent bug invisible against wasp12b (whose facets happen to equal
the tuple) until `corpora/demo/`'s `outcome` facet exposed it by simply not being one of
the four names. The general lesson: a taxonomy-driven feature that still has a facet name
spelled out literally anywhere is a bug waiting for a second corpus to find it. `grep -rn
"research_theme\|role_of_wasp12b" src/papertrail/` should return nothing — if it does,
that's the same class of bug.

## 3. The five generalization seams

Each of these replaced something WASP-12b-specific with a `corpus.yaml`-driven
mechanism. If you're adding "make X configurable per corpus," it almost certainly belongs
in one of these five, not a new mechanism:

| Seam | Module | Configures |
|---|---|---|
| Identity | `identity.py` | Doc labels, reference-list formatting, external links, the uncited-mention heuristic |
| Prompts | `prompts.py` + `prompts/*.txt` | LLM system-prompt voice/subject/citation-example |
| Taxonomy & navigation | `taxonomy.py` | Facets, their presentation, doc-tree grouping, MCP filter params |
| Branding | `Corpus.title`/`glyph`/`description` + `GET /api/corpus` | SPA title/glyph, no build step |
| Config/path resolution | `config.py` | Where a corpus's data lives, env var precedence |

A new corpus should never require touching code in `pipeline/`, `core/`, `web/`, or
`mcp/` — every corpus-specific fact should be expressible through one of these five
seams. If it isn't, that's a real gap (see §7).

## 4. Extension recipes

**Add a taxonomy facet.** Config-only — add an entry under `corpus.yaml`'s `taxonomy:`.
No code change. Verify: `search_fulltext`/`list_papers`'s MCP schemas pick it up
automatically (§7, REFERENCE.md), and `pipeline/index.py` writes `doc_facets` rows for it
(this is exactly what the demo-corpus bug above would have caught, if it existed).

**Add a built-in `CitationStyle`.** Only justified when a corpus's needs exceed what a
`./style.py:ClassName` subclass of `AuthorYearStyle`/`PlainStyle` can express cleanly
(see `corpora/wasp12b/style.py` for the bar: 6 lines, one method overridden). Add the
class to `identity.py`, register it in `_BUILTINS`. Don't generalize the uncited-mention
regex itself without new evidence from a real second corpus's false positives — it's
empirically calibrated on wasp12b's specific prose patterns (see `identity.py`'s comment
on the regex), not a general solution waiting to be found.

**Add a `block_type` to Stage A.** Extend `pipeline/sections.py`'s block-type mapping
(the table in `docs/spec/01_data_contracts.md`) and `core/renderer.py`'s corresponding
render branch. Add a golden-test fixture in `tests/fixtures/marker/` +
`tests/fixtures/expected/` covering it.

**Add an MCP tool.** Simple case (no per-corpus taxonomy dependency): a normal
`@mcp.tool()`-decorated function inside `build_server(settings)`, closing over
`settings` like the existing static tools (`get_paper`, `get_section`, etc.). If the
tool needs one parameter per taxonomy facet (like `search_fulltext`/`list_papers`), it
must be built as a plain function with `**filters`, given a runtime `__signature__` via
`_bind_signature()`, and registered with `mcp.add_tool(fn, name=..., description=...)`
— **not** the `@mcp.tool()` decorator, which runs at function-definition time, before
there's a taxonomy to build a signature from.

**Add an HTTP endpoint.** A normal FastAPI route inside `create_app()`'s factory body.
If it needs corpus branding/facet data, it can reuse the same `settings.corpus.taxonomy`/
`navigation` properties `GET /api/corpus` already exposes rather than recomputing them.

**Add a prompt template.** Add the `.txt` file under `src/papertrail/prompts/`, add its
name to `prompts.py`'s `_KNOWN_TEMPLATES` set (`render()` rejects anything not in this
set), and call `prompts.render(corpus, "your_template.system", **extra_vars)` from
whichever stage needs it. Remember: only *system* prompts are templated — user-content
assembly (node pools, body text) stays as plain Python string building in the calling
stage, deliberately, so a corpus overriding a template can't accidentally break the
node-pool contract that stage depends on.

## 5. Two data-flow traces

**A citation's life.** Stage A assigns a `node_id` when it builds the section tree ⟶ the
node pool (id + text preview) is shown to an LLM (Stage C's per-heading prompt), or the
browser agent reads nodes through MCP ⟶ the model emits an `[n]` marker plus a
`{id, doc_id, node_id}`
citation entry ⟶ Stage C writes `<doc_id>.citations.json` alongside the wiki page; chat
passes the raw payload straight to `validate_and_enrich()` ⟶ the validator resolves
`(doc_id, node_id)` against the registry's `Doc.node_by_id`, checks `has_fulltext`, and
enriches the citation with `reference_label`/`external_links`/`section_title` ⟶ the SPA's
chip-injection code (a `TreeWalker` over the rendered HTML, skipping `CODE`/`PRE`/`A`/
`BUTTON` so literal `[1]` in code/links survives) turns `[n]` text into a clickable
element ⟶ clicking it calls `GET /api/documents/{doc_id}/sections/{node_id}` to fetch and
scroll to the exact node.

**A chat request.** `POST /api/chat` calls the service selected by
`create_agent_chat_service()` (`web/agent_chat.py`). `AGENT_BACKEND=langchain` lazily
discovers the existing MCP server's tools, keeps only targeted search, section-read, and
figure-read tools, converts the last 12 stored messages to model context, and runs
LangChain's LangGraph-backed `create_agent`. `AGENT_BACKEND=codex` runs an ephemeral
official Codex SDK thread with the same Papertrail-only MCP allowlist and disables shell,
web, general filesystem access, and persistent Codex history. Model/tool rounds are
bounded by the selected backend and an overall timeout. Tool calls become sanitized SSE
step events. The final structured payload must have exact marker/id agreement, and every
cited `(doc_id, node_id)` must have been successfully fetched through `get_section` in
that attempt. One correction attempt is allowed; then `validate_and_enrich(strict=True)`
runs in trusted backend code and only a clean result is persisted.

## 6. Testing

```bash
pip install -e '.[dev]'
python -m pytest tests/
```

| File | Covers |
|---|---|
| `conftest.py` | Nothing functional — documents that tests import from the installed package (`pip install -e .`), no `sys.path` hacks needed |
| `test_build_sections.py` | Stage A golden-file regression: 3 fixture Marker JSONs → tmpdir → byte-for-byte diff against committed expected JSON. Also covers figure-image extraction to an explicit `assets_dir` (never the real corpus), reference-node source-block-id sharing, unknown block types recursing defensively, dropped block types producing no nodes. |
| `test_answer.py` | `validate_and_enrich` + `find_uncited_author_year_mentions`: citation enrichment, unknown doc_id (both the `offending` list and strict-mode raise), metadata-only doc rejected even with a real doc_id, unknown node_id, non-strict mode dropping offenders as warnings, missing/unused `[n]` marker warnings, the author-year heuristic (parametrized true/false-positive cases) and its window, and that `PlainStyle` disables the heuristic entirely. |
| `test_registry_context.py` | `core/nodes.py`'s pure context-escalation helpers against a synthetic tree, no corpus load needed: leaf vs. top-level-container lookup, not-found, subtree concatenation, `context=0` prefixing with the parent title, expansion within a section with the cited node bracketed, clamping at section boundaries, container siblings contributing title only. These are the semantics behind MCP's `get_section(context=N)`, used by browser and external agents. |
| `test_agent_chat.py` | Browser-agent grounding rules, history conversion, provider selection, Codex tool lockdown/MCP auditing, strict persistence, one correction attempt, and terminal error behavior without a live model. |
| `test_live_smoke.py` | Live HTTP smoke against a **running** webapp on `WEBAPP_TEST_PORT` (default 8009): health, documents, one document, search. Self-skips entirely if nothing answers on the port, so it stays green on a fresh clone or in CI with no server running. |
| `fixtures/marker/*.json` / `fixtures/expected/*.json` | Input/expected pairs for the Stage A golden tests. |

**Honest coverage gaps** — nothing currently tests: `config.py`'s corpus-root resolution
precedence or env-path override chain; any of `taxonomy.py`'s three `TaxonomyConfigError`
paths or its omission-defaults; `prompts.py`'s `prompts_dir` override, strict-substitute
failure mode, or version-hash normalization; `identity.py`'s external-link rendering
(percent-encoding, `:raw`, empty-field drops); the CLI dispatch table; the MCP dynamic
signature builder; or any pipeline stage past A. `corpora/demo/` is, in practice, the
end-to-end integration test for all of the above — but it's run by hand, not from
`tests/`. Turning a `--corpus corpora/demo` pipeline run into an automated (if slow/paid)
integration test is a reasonable next contribution.

`.migration_baselines/` (gitignored) holds frozen pre-extraction outputs (section-JSON
checksums, sqlite row/content hashes, endpoint JSON checksums, MCP tool text output) used
to verify each migration step was byte-identical or intentionally-different where
expected. Not part of the test suite; a manual verification artifact from the extraction
itself.

## 7. Known gaps and deliberate non-decisions

Don't "fix" the deliberate ones without a real reason — they're documented choices, not
oversights:

- **`source.doc_id_field` and the whole `models:` corpus.yaml block are dead — zero code
  readers.** Both shipped corpora set them anyway, with comments explaining they're
  informational only. A real cleanup candidate, but deleting config keys a hypothetical
  reader might be relying on for documentation purposes is a semantic call, not a
  doc-writing one — left as-is pending an actual decision to remove them.
- **`wiki_headings[].guidance` is parsed but never interpolated into any prompt.**
  Reserved for future use per its own docstring — don't expect it to have an effect.
- **wasp12b's `method` facet has `similarity_weight: 0`**, deliberately reproducing
  pre-extraction behavior (`method` was never scored for paper-similarity, likely an
  original oversight, preserved on purpose rather than silently "fixed" during the
  extraction). Changing it to a nonzero weight is a real behavior change, fine to make,
  but should be its own deliberate commit, not incidental to an unrelated change.
- **No cross-tab push.** `publish_answer`/`POST /api/answers` return a stable `{id,
  url}`; there is no SSE/WebSocket channel notifying an already-open browser tab that a
  new answer exists. A deliberate scope decision (see `docs/spec/00_overview.md`'s
  non-goals), not a missing feature.
- **The uncited-mention heuristic is calibrated on one corpus's prose.** Its regex and
  200-character window come from a real 40-page sample of wasp12b's LLM-generated wiki
  pages. Extending it (or its window) for a different prose style should be new evidence
  from that corpus's actual false-positive rate, not a guess.
- **`pydantic` is imported directly by `mcp/server.py` but not declared** as a
  dependency — it arrives transitively and reliably via `openai` (a core dependency), so
  this has never actually failed, but it's technically an implicit guarantee rather than
  a declared one.
- **Packaged non-Python data** (`prompts/*.txt`, `web/static/index.html`) ship via
  hatchling's default "include everything under `packages`" behavior — there's no
  explicit `force-include`/`artifacts` pin. If anyone adds a build-time exclude rule or
  switches build backends, these can silently stop shipping with no error until someone
  notices prompts/the SPA are missing from an installed wheel.
- **No `[tool.ruff]` or `[tool.pytest.ini_options]` in `pyproject.toml`** — `ruff` and
  `pytest` are declared as dev dependencies but unconfigured; house style (the code is
  visibly ~100 columns) isn't machine-enforced.

## 8. Relationship to `docs/spec/`

`docs/spec/` predates the papertrail extraction — it's a design-rationale record for the
original, WASP-12b-specific `kb/`/`webapp/` implementation this package was extracted
from. It is **not** re-written to match papertrail's current code; that would destroy its
value as a record of *why* the system is shaped the way it is (05_adoption.md's own
preamble, narrating how its previous genericity claims turned out false against the
actual code, is the single best argument in this repo for why the extraction happened at
all). Each file below has a "migration note" callout pointing at its papertrail
equivalent; this table is the authoritative summary of what's still true in each vs. what
this doc set supersedes.

| Spec doc | Still authoritative for | Superseded by |
|---|---|---|
| `00_overview.md` | The answer-payload-as-one-artifact design principle; the two reference implementations papertrail's patterns were adapted from; corpus completeness (`has_fulltext` full-text-vs-stub distinction) | README.md's pitch; this guide's §2 invariants |
| `01_data_contracts.md` | The section-tree node model, `block_type` table, assembly algorithm, worked examples — the deepest and most reusable doc in the set, genuinely still the reference for Stage A's output shape | REFERENCE.md §8-9 (SQLite schema, answer payload) for the parts that changed (generic `doc_facets` vs. one-column-per-facet; `reference_label`/`external_links` vs. `author_year`/`ads_url`) |
| `02_pipeline.md` | Per-stage algorithm descriptions (retry logic, similarity scoring rationale, cost characteristics) | REFERENCE.md §1 (full flag reference); USERS_GUIDE.md §8 (task-level "how to run each stage") |
| `03_webapp.md` | The chat SSE step-flow design rationale, why there's no cross-tab push | REFERENCE.md §6 (current endpoint table + SSE grammar — the old table is missing `/api/corpus` and `/api/reload`) |
| `04_mcp.md` | The agent authoring workflow (search → read → cite → validate → publish) | REFERENCE.md §7 (current tool signatures — the old table hardcodes wasp12b's four facet names as if every corpus has them, which is now flatly wrong, not just stale) |
| `05_adoption.md` | The generic-vs-corpus-specific argument table; the two-tier adapter framing (wrap an existing loader vs. derive from Marker JSON) | USERS_GUIDE.md (the practical, current checklist); REFERENCE.md §3 (the field-by-field schema) |

If you find a spec doc's stale content actively misleading a reader (not just outdated),
add or extend its migration-note callout rather than rewriting the surrounding
design-rationale prose.
