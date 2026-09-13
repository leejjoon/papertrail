# PDF-to-Browser Quickstart

Convert one local PDF to Datalab Marker JSON, build Papertrail's section tree and
full-text index, and read the result in a browser. This path does not call the LLM
classification or wiki-generation stages; only the Datalab conversion uses a remote API.

The commands below use this repository's existing `.venv` exclusively.

## 1. Prerequisites

- Python 3.11 or newer in `.venv`.
- `uv` available as the package installer.
- A root `.env` containing the Datalab key and one chat backend configuration.
  For an OpenAI-compatible gateway such as BizRouter:

```dotenv
DATALAB_API_KEY=your-key-here
BASE_URL=https://your-gateway.example/v1
API_KEY=your-model-key
# Optional; defaults to CHAT_MODEL and then Papertrail's built-in model name.
AGENT_MODEL=your-tool-calling-model
AGENT_BACKEND=langchain
```

Or use your existing Codex login instead of a gateway API key:

```dotenv
DATALAB_API_KEY=your-key-here
AGENT_BACKEND=codex
# Optional; empty uses the model selected by your Codex account/configuration.
CODEX_MODEL=
```

Authenticate once with `codex login` (or `codex login --device-auth` on a headless
machine). Papertrail delegates login and token refresh to the official Codex SDK and
does not read OAuth tokens itself. This mode is intended for a private, single-user
reader; do not expose a subscription-authenticated server as a public multi-user API.

Install Papertrail and its Datalab, web, and development dependencies into `.venv`:

```bash
uv pip install --python .venv/bin/python -e '.[all,dev]'
```

Do not invoke system `python`, `pip`, `papertrail`, or `uvicorn` for this workflow.

## 2. Initialize and ingest in one command

For a new single-PDF corpus, no YAML needs to be written manually. Pass an empty or
nonexistent corpus directory with `--init-corpus`:

```bash
.venv/bin/papertrail ingest-pdf path/to/paper.pdf \
  --corpus corpora/my-paper \
  --init-corpus
```

This creates a minimal `corpus.yaml`. After Datalab conversion succeeds, the same
command creates `documents.yaml`, section and image artifacts, and the SQLite index.
Use `--corpus-title "My Paper Reader"` to override the browser title; otherwise it is
derived from the PDF filename. Existing `corpus.yaml` files are never overwritten.

For custom branding or identity settings, the generated configuration can be edited
before adding more PDFs. It has this shape:

```yaml
# corpora/my-paper/corpus.yaml
corpus:
  name: my-paper
  title: paper Paper Reader

source:
  marker_dir: data/marker
  documents_file: documents.yaml

identity:
  citation_style: plain

navigation:
  year_pages: false

wiki_headings: []
```

The generated manifest contains the document metadata. Optional `--title`, `--authors`,
`--year`, `--journal`, and `--fulltext-source` arguments populate it; without them,
Papertrail derives the document title from Datalab's first heading and leaves optional
fields empty.

```yaml
# corpora/v346nor/documents.yaml
documents:
  - doc_id: V346draft_20260901
    title: A candidate SiO jet tracing the outburst history of the FUor-type star V346 Nor
    authors: [Ji-hyun Kang, Jeong-Eun Lee, Jae-Joon Lee]
    year: 2026
    journal: Journal of the Korean Astronomical Society
    has_fulltext: true
    fulltext_source: author_draft
    extra: {}
```

The `doc_id` may contain letters, digits, `.`, `_`, `+`, and `-`. If omitted, it defaults
to the PDF filename stem.

## 3. Convert and index the included V346 Nor PDF

From the repository root, run:

```bash
.venv/bin/papertrail ingest-pdf test_paper/V346draft_20260901.pdf \
  --corpus corpora/v346nor \
  --doc-id V346draft_20260901 \
  --mode balanced
```

This one command:

1. uploads the PDF to Datalab and polls until conversion completes;
2. saves the complete Marker JSON response;
3. builds Papertrail's citable section tree and extracts figure images; and
4. atomically rebuilds the corpus's SQLite/FTS5 index.

Successful output for the included draft ends with:

```text
Indexed: 1 full-text papers + 0 stub docs = 1 docs, 172 nodes, 0 facet rows
Ready: V346draft_20260901 — 172 nodes, 69 references
```

The conversion is resumable. Re-running the same command with an unchanged PDF reuses
the saved Marker JSON and does not spend another Datalab request. Use `--force` only
when you intentionally want to reconvert it.

## 4. Start agentic chat and the browser reader

Agentic chat uses the same Papertrail tools available to external MCP clients. Start
the private MCP sidecar in one terminal; the browser agent itself allowlists only the
targeted `search_fulltext`, `get_section`, and `get_figure_image` tools:

```bash
.venv/bin/papertrail mcp \
  --corpus corpora/v346nor \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 8013
```

Keep MCP bound to `127.0.0.1`; it does not need to be exposed to the network. In a
second terminal, start the reader:

```bash
.venv/bin/papertrail serve \
  --corpus corpora/v346nor \
  --host 127.0.0.1 \
  --port 8012
```

Open [http://127.0.0.1:8012](http://127.0.0.1:8012). Select the paper under the
“(uncategorized)” group to browse its outline, rendered text, tables, equations, and
figures. The search box queries extracted nodes; chat connects to MCP and lets the
configured model search and read multiple sections before producing strictly validated,
clickable citations. For example, ask `What evidence supports the candidate SiO jet?`
The header's agent pill shows the configured backend and its last observed state. Click
it to see the safe runtime details: model selection, authentication type, corpus, and
document count. `Not checked` means no chat request has verified the backend and MCP
connection since the reader process started.

With `AGENT_BACKEND=codex`, each attempt uses an ephemeral, read-only Codex thread.
Shell, web search, filesystem-image access, persistent Codex history, and every MCP tool
except Papertrail's three allowlisted research tools are disabled. Set `CODEX_BIN` only
if the `codex` executable is not on `PATH`; `CODEX_MODEL` is optional.

The single-PDF workflow skips the LLM-backed B–D stages. If you later add taxonomy and
wiki configuration and run the full pipeline, `PIPELINE_BACKEND=codex` also routes B,
C, and D through the cached Codex login; it is independent of `AGENT_BACKEND`.

Keep both terminals running while using chat. The reader and search remain available if
MCP is stopped, but chat reports that its research tools are unavailable. Stop either
server with `Ctrl+C`.

## 5. Generated artifacts

For document ID `V346draft_20260901`, the workflow creates:

| Artifact | Path |
|---|---|
| Raw Datalab response | `corpora/v346nor/data/marker/V346draft_20260901.json` |
| Papertrail section tree | `corpora/v346nor/data/sections/V346draft_20260901.json` |
| Figure images | `corpora/v346nor/data/assets/V346draft_20260901/` |
| Resume metadata | `corpora/v346nor/data/ingest_manifest.json` |
| Search index | `corpora/v346nor/data/kb.sqlite` |

Generated `corpora/*/data/` content is gitignored. Corpus configuration and document
metadata remain trackable.

## Troubleshooting

| Symptom | Resolution |
|---|---|
| `DATALAB_API_KEY is missing or empty` | Add the key to the repository-root `.env`, then rerun the command. |
| Datalab submission or polling fails | Check network access and the API key; no incomplete Marker result is installed. |
| `not a readable PDF` | Confirm the path exists and the file begins with the PDF signature. |
| Invalid `doc_id` | Use only letters, digits, `.`, `_`, `+`, and `-`, or omit it to use the filename stem. |
| Search index is missing | Run `ingest-pdf` successfully, or run `.venv/bin/papertrail index --corpus PATH`. |
| Chat says the research tools are unavailable | Start the MCP command from section 4 and confirm port 8013 is free. |
| Chat cannot complete any question | Set `AGENT_MODEL` to a model that supports OpenAI-compatible tool calls and structured output through your gateway. |
| Codex chat says it is not signed in | Run `codex login` (or `codex login --device-auth`), then restart `papertrail serve`. |
| Codex dependencies are missing | Install them into the project environment with `uv pip install --python .venv/bin/python -e '.[codex]'`. |
| Codex cannot find its runtime | Install the Codex CLI or set `CODEX_BIN` to its executable path. |
| Agent times out | Ask a narrower question or raise `AGENT_TIMEOUT_SECONDS` from its 120-second default. |
| Figures return 404 after ingestion | Restart `papertrail serve`; the asset mount is established when the server starts. |
| Port 8012 is occupied | Choose another port and open that same port in the browser. |

For all configuration fields and CLI details, see [REFERENCE.md](REFERENCE.md). For the
full classification and cited-wiki pipeline, see [QUICKSTART.md](QUICKSTART.md).
