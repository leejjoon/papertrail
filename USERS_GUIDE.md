# Users Guide: Adopting `papertrail` for Your Corpus

This is a task-organized guide for pointing `papertrail` at your own documents. Every
field/flag/endpoint table it references lives in [REFERENCE.md](REFERENCE.md) — this
document explains *why* and *how*, REFERENCE.md is the *what exactly*. For a 15-minute
working example before reading all of this, see [QUICKSTART.md](QUICKSTART.md). For
design rationale behind decisions this guide states as given, see `docs/spec/` (written
for the original single-corpus implementation this was extracted from, but the reasoning
mostly still applies — see `DEVELOPERS_GUIDE.md` for exactly what's still authoritative
in each spec doc).

## 1. Mental model

Four artifact layers, each depending only on the one before it:

```
Marker JSON  →  section tree  →  classification  →  wiki pages  →  SQLite index
  (yours)      (Stage A)          (Stage B)          (Stage C/D)     (Stage E)
```

**The section tree is the only citable ground truth.** Every citation anywhere in the
system — a wiki page's `[n]`, a chat answer's citation, an agent's `publish_answer` call —
resolves to a `(doc_id, node_id)` pair pointing into a section tree that Stage A produced
directly from your Marker JSON, never into another wiki page's prose. A topic page never
cites a paper page's `[n]`; it cites the same underlying node the paper page cited. This
is why Stage A defines the entire granularity/addressing scheme everything downstream
inherits: a node is a paragraph, one table, one figure, one equation, one footnote, or
one bibliography entry, and the whole document reconstructs exactly once by concatenating
every node in document order (a container node holds only its own heading text, never
its children's text redundantly).

Everything that makes a claim and backs it with sources — a wiki page, a chat answer, an
agent-published answer — is validated by the same function,
`validate_and_enrich()` (`core/citations.py`). This is not four similar-looking
validators; it's one function four different producers call.

## 2. What a corpus directory is

```
my-corpus/
  corpus.yaml           # required — everything below is driven by this file
  adapter.py            # or wherever source.metadata_adapter points
  style.py               # optional, custom CitationStyle
  prompts/                # optional, prompts_dir override
  .env                    # optional, corpus-specific credentials/path overrides
  data/                    # created by pipeline stages (--corpus resolution + create_dirs=True)
    sections/ assets/ classification/ kb.sqlite
  wiki/
    papers/ topics/ index.md
```

The corpus root is **never** inside the installed `papertrail` package — `corpora/
wasp12b/` and `corpora/demo/` in this repo are two examples, but a corpus can live
anywhere on disk (`~/my-corpus/`, a mounted volume, wherever). For an existing directory
of Marker JSON, Papertrail starts directly at Stage A. For a single local PDF,
`papertrail ingest-pdf FILE --corpus PATH` provides the supported Datalab conversion
path and immediately runs Stage A plus Stage E. The older `paper_download/` scripts
remain corpus-specific batch acquisition tooling.

## 3. Corpus resolution and `.env`

`load_settings()` finds your `corpus.yaml` via, in order: an explicit `--corpus PATH` →
`PAPERTRAIL_CORPUS` (or legacy `KB_DIR`) → an upward search from the current directory →
a hard error naming all three options. There's no implicit default corpus — a command run
from the wrong directory with no `--corpus`/env var fails loudly rather than silently
picking up a stray `corpus.yaml`.

`.env` loads from two places, both **before** any other environment variable is read:
your corpus root's own `.env` first, then the nearest `.env` found walking up from the
current directory (`find_dotenv(usecwd=True)`). Neither overrides a variable already
exported in your shell. Practical implication: if you run pipeline commands from inside
the corpus directory, one `.env` there covers everything; if you run from elsewhere, keep
a `.env` in your working tree too, or export the variables directly.

Full env var reference: [REFERENCE.md §2](REFERENCE.md#2-environment-variables).

## 4. Writing `corpus.yaml`

Full field-by-field table (required/optional, defaults-when-omitted):
[REFERENCE.md §3](REFERENCE.md#3-corpusyaml-schema). This section is about the choices,
not the field dump.

**Start minimal and add.** Every block except `source:` is optional and has a sane
default — `corpora/demo/corpus.yaml` is a complete, working 68-line example with 2
facets, 3 headings, no external links, and no custom citation style, deliberately built
to be as different from `corpora/wasp12b/corpus.yaml`'s 100 lines as a real second corpus
plausibly would be.

**`corpus.subject`** is the one field every LLM prompt actually depends on — it's
literally interpolated as "a knowledge base about $subject." Write a noun phrase, not a
sentence: `"the exoplanet WASP-12b"`, `"hot Jupiter exoplanets"`. `citation_example.bad`/
`.good` is the one example every prompt shows the model of "don't just name-drop an
author-year, attach `[n]` to that exact sentence" — write it in your corpus's own idiom
if `Author et al. (YYYY)` doesn't match your source material's citation convention.

**Taxonomy is a closed vocabulary, not a suggestion.** Every facet's `values:` list is
what the classifier LLM is told it may choose from (`classify.system.txt`'s prompt says
"use ONLY the listed values, never invent one"), and it's also what builds the MCP
server's filter enums (§7, REFERENCE.md §7) — so a facet with `role_of_wasp12b: [primary-
target, incidental-mention]`-style values gets a `role_of_wasp12b` parameter on
`search_fulltext`/`list_papers` with exactly those two choices, automatically, no code
change. Decide facet count and cardinality (`multi: true/false`) based on how you want
papers grouped and filtered — there's no fixed number, wasp12b has four, demo has two.
`similarity_weight: 0` on a facet means "classify it, but don't let it influence Stage
C's related-papers scoring" — a legitimate choice for a facet that's informative to
browse by but not meaningfully similarity-bearing (wasp12b does this for `method`,
reproducing pre-extraction behavior deliberately, not as an oversight to copy blindly).

**Eager validation is a feature, treat its errors as useful, not annoying.** `corpus.yaml`
is checked at load time, not at first use several stages later:
- Two `topic_pages: true` facets sharing a `slug_prefix` → immediate
  `TaxonomyConfigError` naming the collision, instead of one facet's topic pages silently
  overwriting the other's output files.
- `navigation.tree_facet` or any `navigation.table_columns` entry naming an undeclared
  facet → immediate error listing exactly which name(s) are unknown.
- `navigation.digest_headings` naming something not in `wiki_headings` → immediate
  error. This one has a real history: before this validation existed, renaming a wiki
  heading without updating the digest list silently broke every topic page with the
  misleading message "no member paper has a wiki page yet" — which doesn't even mention
  headings. If you rename a heading, the loader now tells you immediately what you forgot
  to update.

**`year_pages: false` is supported** for a corpus whose adapter never supplies a `year`
(the demo corpus does this) — don't feel obligated to invent a year.

## 5. Writing a metadata adapter

Contract and both reference examples in full:
[REFERENCE.md §4](REFERENCE.md#4-adapter--citationstyle-interfaces). Two tiers, pick
based on what you already have:

**Tier 1 — you have an existing loader** (a database, a metadata YAML/CSV, an API).
Wrap it: your `load()` function calls into that loader and reshapes each record into the
adapter contract's dict shape. `corpora/wasp12b/adapter.py` does exactly this — it wraps
`paper_download/config.load_papers()` (which itself reads a YAML file plus an ADS-fetched
metadata JSON), adding `has_fulltext`/`fulltext_source` computed from a separate
PDF-fetch manifest. No new metadata-loading logic, just reshaping.

**Tier 2 — you have nothing but a directory of Marker JSON.** Derive everything from the
Marker JSON itself: `doc_id` = filename stem, `title` = the first heading block's text
(strip HTML tags), leave `authors`/`year`/`journal`/`native_id` unset. `corpora/demo/
adapter.py` is a complete ~70-line example of this tier. You lose author-year citation
labels (use `identity.citation_style: plain`, §6) and external links (omit `identity:`
entirely, or the whole `external_links:` sub-key), but the pipeline runs identically
otherwise — nothing downstream requires the richer metadata.

**`has_fulltext` is the one field worth understanding precisely.** It's three-valued:
`True` (has a Marker JSON, gets the full pipeline), `False` (deliberately excluded —
Stage A skips it, Stage E gives it a metadata-only "stub" `docs` row with one searchable
node and no wiki page), and `None`/absent (Stage A falls through to checking whether a
matching Marker JSON file actually exists on disk — the right default for a minimal
adapter that doesn't track this explicitly). A stub doc can never be cited — `has_fulltext
= 1` is a hard requirement in `validate_and_enrich`'s citation-resolution check, not just
a display flag.

## 6. Identity and citation style

Full protocol and builtin comparison table: [REFERENCE.md §4](REFERENCE.md#4-adapter--citationstyle-interfaces).

Three options, in order of how much you should reach for each:

1. **Do nothing — omit `identity:` entirely.** You get `AuthorYearStyle` with no
   external links: labels fall back to `doc_id`, reference labels to a title snippet, no
   links rendered anywhere. Fine for a first pass on any corpus.
2. **`citation_style: plain`** when your corpus has no author-year citation convention
   worth enforcing — a minimal adapter with no `authors` field, or source material that
   isn't academic papers at all. This also fully disables the uncited-author-year-mention
   heuristic (`mention_pattern()` returns `None`), which matters: that heuristic is
   calibrated on wasp12b's specific prose patterns (empirically: legitimate citations
   observed 100–345 characters after an author-year mention), and running it against
   prose it was never tuned on will false-positive constantly. Don't leave `author_year`
   selected "just in case" for a corpus without real author-year citations in its prose.
3. **A custom subclass** (`citation_style: ./style.py:YourClassName`) only when neither
   builtin fits and you need to override specific behavior — `corpora/wasp12b/style.py`
   is the calibration for how small this should be: 6 lines, one method
   (`canonicalize`) overridden, to also try a `&`↔`+` filesystem-escape alias when
   resolving a doc_id. If you're writing more than that, double check you actually need a
   custom style rather than a `plain`/`author_year` choice plus adapter-level fixes.

**External links are declarative, with one rule worth internalizing:** a link template
referencing any field that's empty or absent for a given doc is silently dropped, not
rendered as a broken link. This is the entire mechanism behind "no bibcode → no ADS
link" — there's no separate conditional syntax to write. If you want a link that always
appears, only reference fields you're certain every doc supplies.

## 7. Prompts and the subject seam

Templates ship as package defaults under `src/papertrail/prompts/*.txt`, six files:
`classify.system`, `paper_page.system`, `topic_page.system`, `index_intro.system`,
`agent.system`. Every one is `string.Template`-rendered with strict
substitution — a template variable your corpus.yaml doesn't supply raises a `KeyError`
immediately at generation time rather than silently emitting a literal `$var` into a paid
LLM call.

`corpus.subject` and `corpus.citation_example.{bad,good}` are the only variables *you*
supply (§4 above); everything else (`$facet_lines`, `$heading_list`,
`$inapplicable_clause`, etc.) is computed by the calling pipeline stage from your
taxonomy/headings and passed through automatically — you never write these yourself.

**One residual leak worth knowing about:** `classify.system.txt`'s opening sentence is
literally *"You classify astronomy papers for a knowledge base about $subject."* — the
word "astronomy" is not templated. For a non-astronomy corpus, override this one file
(see below) rather than assume `$subject` alone neutralizes it.

**`prompts_dir:` replaces a template wholesale, per-file, no inheritance.** Set
`prompts_dir: prompts` in `corpus.yaml`, then drop a `prompts/classify.system.txt` (etc.)
in your corpus directory — that file entirely replaces the packaged default for that one
template name; the other five templates you don't override still come from the package.
Your override must still supply every `$variable` the calling stage passes in, or
rendering raises.

**Prompt versioning is automatic and matters for cost control.** `stage_prompt_version()`
hashes the fully-rendered system prompt (template text + your `subject` + taxonomy +
headings, whitespace-normalized) into one short version string, stored alongside each
generated classification/wiki page. Editing `corpus.yaml`'s wording, taxonomy, or
headings changes this hash; a plain re-run leaves already-generated docs alone (reported
as "classified with a different prompt version" rather than silently stale), and
`--regenerate-if-prompt-changed` is the explicit opt-in to actually redo them. This is why
a cosmetic `.env`/formatting edit doesn't force a corpus-wide re-run: whitespace is
normalized out of the hash.

## 8. Running the pipeline

Full per-stage flag table: [REFERENCE.md §1](REFERENCE.md#1-cli).

| Stage | Command | LLM? | What it does |
|---|---|:-:|---|
| A | `papertrail sections --corpus PATH` | no | Marker JSON → section-tree JSON + figure images. Fully deterministic, resumable via a marker-file sha256 check. |
| B | `papertrail classify --corpus PATH` | `BULK_MODEL` | Tags each paper against your taxonomy. |
| C | `papertrail paper-pages --corpus PATH` | `BULK_MODEL` | Generates each paper's cited wiki page, one heading at a time, validate-and-retry per heading. |
| D | `papertrail topic-pages --corpus PATH` | `SYNTH_MODEL` | Cross-paper topic pages (one per `topic_pages: true` facet value) + the wiki index. |
| E | `papertrail index --corpus PATH` | no | Rebuilds `kb.sqlite`'s content tables from everything above. |

Run them in order; each is independently resumable (`--dry-run` shows what's left,
`--only`/`--limit` scope a run, `--force` bypasses the resume check).

Stages B–D use the OpenAI-compatible gateway by default. To run all three through the
official Codex SDK and the cached ChatGPT login instead, set `PIPELINE_BACKEND=codex`,
optionally set `PIPELINE_CODEX_MODEL`, and run `codex login` once. This mode does not
need `API_KEY`. It defaults B/C to two parallel requests to use subscription allowance
conservatively; `--workers N` remains an explicit override. Changing providers does not
invalidate existing outputs automatically, so use `--force` when intentionally
regenerating them with Codex.

**Stage A diagnostics worth reading, not ignoring:** a paper reported with zero
`reference` nodes is a likely sign its bibliography's `ListGroup` block didn't parse as
expected — worth eyeballing. A paper reported with more than 60 pages is the same
wrong-document heuristic used during acquisition, worth a second look.

**Stage C's `--frontmatter-only`** re-derives a paper page's frontmatter and `## Links`
footer from current metadata/classification while leaving the LLM-generated body
markdown byte-for-byte untouched — zero LLM calls, no `API_KEY` needed. Use this after
a metadata correction or taxonomy relabeling that shouldn't cost a re-generation, but
only touches docs that already have a generated page; it can't create one.

**Stage D always lints on a normal run and exits nonzero on a broken link** — a
successful generation with one dangling relative link in the wiki still fails the
command. Run `--lint` alone (no generation) as a standalone check any time.

**Cost shape**, roughly: Stage A and E are free (no API calls). Stage B is one cheap call
per paper. Stage C is one call per paper per heading, with retries — the largest call
count in the pipeline. Stage D is a handful of calls (one per facet value with
`topic_pages: true`, plus the index) but each is the more expensive `SYNTH_MODEL`.

## 9. Serving the web app

```
papertrail serve --corpus /path/to/corpus --port 8000
```
Deployment tools may instead launch `uvicorn papertrail.web.asgi:app`; that module-level
entry point resolves the corpus through `PAPERTRAIL_CORPUS`/`KB_DIR`/upward-search (§3).

Set `ROOT_PATH=/some-prefix` if deploying behind a reverse proxy at a path prefix — it's
threaded explicitly into every rendered `<img src="...">` URL, which uvicorn's own
`--root-path` flag does not fix (that only corrects routing/OpenAPI, not URLs already
baked into rendered HTML).

After a pipeline re-run, `POST /api/reload` drops the server's caches and re-reads
`kb.sqlite`/`corpus.yaml` — no process restart needed. One caveat: the `/assets` static
mount is decided once, at server-factory time, based on whether `assets_dir` exists at
that moment. If you run Stage A (which creates `assets_dir`) *after* the server has
already started, figures 404 until you restart the process — `POST /api/reload` doesn't
fix this particular case.

The dropdown beside the Send button chooses which research agent answers the next
question: LangGraph (an OpenAI-compatible gateway with `API_KEY`), Codex (your `codex
login`), or Claude (your Claude Code login, `claude auth login`). The server's
`AGENT_BACKEND` is preselected; backends whose extra is not installed are greyed out,
and the choice is remembered in the browser. Every backend goes through the same
read-before-cite validation, so answers from any of them carry the same guarantees.

The agent pill in the header reports the selected chat backend and its last observed
state. Click it for the configured model, authentication type, corpus, and document
count. `Not checked` immediately after startup is expected: Papertrail does not initiate
an OAuth flow or model request merely to render the page. The pill refreshes after each
chat request.

## 10. Running the MCP server

```
papertrail mcp --corpus /path/to/corpus
```
or the equivalent `.mcp.json` registration (full shape in
[REFERENCE.md §7](REFERENCE.md#7-mcp-tools)). Defaults to stdio transport;
`MCP_TRANSPORT=http` runs a stateless Streamable-HTTP service instead
(`MCP_HOST`/`MCP_PORT`), or pass `--transport streamable-http --host ... --port ...`
directly. `WEBAPP_BASE_URL` controls the URL `publish_answer` hands back
to whatever agent called it — set it to wherever your web app is actually reachable, or
published-answer links will point at the wrong place.

For an agent authoring an answer through this server, the loop is: `list_papers()`/
`search_fulltext()` to scope relevant papers, `get_section`/`get_paper`/
`get_figure_image` to actually read content before citing it, draft prose with `[n]`
markers, `validate_answer()` in a loop until clean, `publish_answer()`. This repo's
`.claude/skills/wasp12b-answer/SKILL.md` is a worked example of packaging that loop as an
agent-invocable skill for one specific corpus.

## 11. The citation contract, from a user's perspective

Two genuinely different checks happen on every answer payload — don't conflate them:

1. **Every `[n]` marker used must resolve to a real `(doc_id, node_id)`.** Hard check;
   raises in strict mode (`validate_answer`/`publish_answer`), becomes a warning and gets
   silently dropped in lenient mode (`POST /api/answers?strict=false`). Browser chat
   uses strict mode and refuses to persist an invalid result.
2. **Every author-year mention should have an `[n]` nearby.** A heuristic, warnings only,
   never a hard gate — and entirely disabled under `PlainStyle`. It exists because check
   1 alone lets a real failure mode through: a model naming a study by author-year with
   no citation marker attached anywhere produces zero markers to check, so check 1 has
   nothing to catch. This has a real false-positive rate on long, correctly-cited
   sentences — treat a warning as something to look at, not something to loop on forever
   chasing zero.

**A resolving citation does not mean the cited text was read in enough context to
support the claim beside it.** Retrieval is paragraph-granular; a single paragraph read
alone is often ambiguous (an unresolved pronoun/comparison, a term whose exact meaning is
paper-specific). MCP's `get_section(doc_id, node_id, context=N)` widens by `N` sibling
nodes within the same section, and browser chat can call it repeatedly with larger context
within the bounded agent run.
Neither `validate_and_enrich()` check above can catch "read out of context"
— that's a correctness question the citing agent (human or LLM) is responsible for, not
something the validator proves.

## 12. Troubleshooting

| Message | Cause |
|---|---|
| `couldn't locate a corpus.yaml — pass --corpus PATH, set PAPERTRAIL_CORPUS, or run from inside (or below) a corpus directory` | No `--corpus`, no `PAPERTRAIL_CORPUS`/`KB_DIR`, and no `corpus.yaml` found walking up from `cwd`. |
| `no corpus.yaml found at <path>` | `--corpus`/`PAPERTRAIL_CORPUS` pointed at a real directory that just doesn't contain a `corpus.yaml`. |
| `Search index not found — run \`papertrail index\` first.` (MCP tools) | `kb.sqlite` doesn't exist yet at the resolved corpus's `data/` path — run Stage E. |
| `API_KEY not found in .env` | The default `PIPELINE_BACKEND=openai` requires an API key for B/C (except `--frontmatter-only`) and D (except `--lint`). Use `PIPELINE_BACKEND=codex` with `codex login` instead, or configure the gateway key. |
| `Codex is not signed in` | Run `codex login` (or `codex login --device-auth` on a headless host), then retry the pipeline command. |
| `Claude Code is not logged in` | Run `claude auth login` (or set `ANTHROPIC_API_KEY` for API billing), then restart `papertrail serve`. |
| `Claude Agent SDK is not installed` | Install the `claude` extra: `uv pip install --python .venv/bin/python -e '.[claude]'`. |
| `TaxonomyConfigError: duplicate slug_prefix among topic_pages facets: {...}` | Two `topic_pages: true` facets share a `slug_prefix` — give one a distinct value. |
| `TaxonomyConfigError: navigation.tree_facet '...' is not a declared taxonomy facet` | `navigation.tree_facet` (or a `table_columns` entry) names something not under `taxonomy:`. |
| `TaxonomyConfigError: navigation.digest_headings references heading(s) not in wiki_headings: [...]` | A digest heading was renamed/removed in `wiki_headings` without updating `navigation.digest_headings`. |
| `ModuleNotFoundError: No module named 'markdown'` | Fixed as of this doc set (`markdown` moved to core deps) — if you still see this, you're on an older installed version; reinstall. |
| Web app / MCP server "sees" the wrong corpus | Check `PAPERTRAIL_CORPUS`/`KB_DIR` in the actual process's environment, not just your shell's — a long-running process only re-reads env at its own startup, and an upward `corpus.yaml` search from an unexpected `cwd` can silently win over what you intended. |
| Figures 404 after a fresh Stage A run against an already-running web app | The `/assets` mount is decided once at server-factory time (§9) — restart the process. |

For anything not covered here, `docs/spec/01_data_contracts.md` has the full section-tree
node model and worked examples if a citation/rendering question is really about the data
shape rather than the pipeline itself.
