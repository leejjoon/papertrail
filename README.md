# papertrail

Marker JSON → section tree → LLM classification → cited wiki → SQLite/FTS index → web UI
+ MCP server, for any corpus of documents that cite each other — not just one.

Everything the system produces that makes a claim and backs it with sources — a wiki
page, a chat answer, an agent-published answer — is the same kind of artifact: prose with
inline `[n]` markers plus a `citations[]` list mapping each `n` to an exact `(doc_id,
node_id)`. One function, `validate_and_enrich()`, validates all of them. That single
contract, applied consistently everywhere, is what makes this different from a generic
RAG pipeline: a citation here isn't a similarity score attached after the fact, it's a
pointer into a specific paragraph, table, figure, or equation that a reader (or another
agent) can jump straight to.

![The papertrail web app: a document tree, a section-level contents outline, the rendered
paper, and a chat answer whose inline [n] markers link to exact source
nodes](docs/images/webapp.png)

*A corpus of planetary-nebula papers — neither of the two corpora in this repo. The
`[6] [7] [8]` chips in the answer pane each resolve to a specific `(doc_id, node_id)`;
clicking one jumps the reader pane to that paragraph.*

## Pipeline at a glance

| Stage | Command | LLM? | Produces |
|---|---|:-:|---|
| A | `papertrail sections` | no | Section-tree JSON + figure images, from Marker JSON |
| B | `papertrail classify` | cheap | Per-paper taxonomy tags |
| C | `papertrail paper-pages` | cheap | One cited wiki page per paper |
| D | `papertrail topic-pages` | expensive | Cross-paper topic pages + index |
| E | `papertrail index` | no | `kb.sqlite` (SQLite + FTS5) |

For a single local PDF, `papertrail ingest-pdf FILE --corpus PATH` performs the
Datalab conversion, Stage A extraction, and Stage E indexing in one resumable
command. Then run `papertrail serve --corpus PATH` to open the browser reader.

Full flag reference: [REFERENCE.md §1](REFERENCE.md#1-cli). Working example with real
output: [QUICKSTART.md](QUICKSTART.md).

## Three ways to read a corpus, once built

- **A web app with chat** (`uvicorn papertrail.web.asgi:app`) — browse the doc tree,
  search, ask questions with inline-cited streamed answers.
- **An MCP server** (`papertrail mcp`) — 9 tools giving a coding agent the same
  search/read access, plus the ability to draft an answer, validate every citation
  resolves, and publish it so it shows up in the web app with identical citation UX.
- **The generated wiki markdown itself** — `wiki/papers/*.md` and `wiki/topics/*.md` are
  plain, human-readable files; no server required to read them at all.

## Repo layout — a naming note

This repo is `wasp12b_papers`; the installable package inside it is `papertrail`
(`src/papertrail/`). They're not the same thing, and the repo currently holds both:

```
src/papertrail/          the generic, installable engine — everything above
corpora/
  wasp12b/                the first real consumer: ~210 papers about the exoplanet WASP-12b
  demo/                   a throwaway second corpus, deliberately shaped differently,
                          proving the engine isn't secretly WASP-12b-specific
paper_download/           PDF/Marker-JSON acquisition for wasp12b — out of papertrail's
                          scope; its contract starts at "a directory of Marker JSON"
kb/, webapp/              pre-extraction legacy: the original WASP-12b-only implementation
                          papertrail was extracted from. Still the LIVE system serving
                          requests today — see CLAUDE.md for the exact cutover status.
docs/spec/                design-rationale record for the pre-extraction implementation —
                          see DEVELOPERS_GUIDE.md §8 for what's still authoritative there
```

If you're here to use `papertrail`, everything you need is in `src/papertrail/` +
`pyproject.toml` + `tests/`; `kb/`/`webapp/`/`paper_download/`/`corpora/wasp12b/` are one
specific corpus's worth of surrounding context, not part of the package.

## Install

```bash
pip install -e '.[web,mcp]'
```
`web` pulls in FastAPI/uvicorn (the web app); `mcp` pulls in the `mcp` SDK. Either extra
alone works standalone (both need the core dependencies, which include `markdown` — see
`pyproject.toml`'s inline comment for why it isn't just a `web`-extra dependency). `all`
= both.

## Status

One real corpus (`corpora/wasp12b/`, ~210 papers, ~38,500 indexed nodes) and one
deliberately-different proof corpus (`corpora/demo/`, 5 papers) both run end-to-end
through every stage, served over both the web app and the MCP server. A characterization
test suite (32 tests) covers Stage A's extraction algorithm, citation validation, and the
context-escalation helpers — see [DEVELOPERS_GUIDE.md §6](DEVELOPERS_GUIDE.md#6-testing)
for exactly what is and isn't covered. No packaged release exists yet (`version = "0.1.0"`
in `pyproject.toml`, not published anywhere), and the package has not yet been split out
of this repository into its own — `corpora/wasp12b/` still lives alongside
`src/papertrail/` here rather than being pulled in as a dependency.

## Documentation map

| Doc | For |
|---|---|
| [QUICKSTART.md](QUICKSTART.md) | Seeing it work end-to-end in 15 minutes, no prior context needed |
| [PDF_QUICKSTART.md](PDF_QUICKSTART.md) | Converting one local PDF with Datalab and reading it in the browser |
| [USERS_GUIDE.md](USERS_GUIDE.md) | Adopting papertrail for your own corpus, task by task |
| [REFERENCE.md](REFERENCE.md) | Looking up an exact CLI flag, config key, endpoint, or tool signature |
| [DEVELOPERS_GUIDE.md](DEVELOPERS_GUIDE.md) | Modifying papertrail itself |
| `docs/spec/*.md` | *Why* the system is shaped this way — a design-rationale record from before the extraction, not a current reference (see DEVELOPERS_GUIDE.md §8 for what's still true in each file) |
| `CLAUDE.md` | This repo's specific live-vs-migrated operational state — not about papertrail as a package |
