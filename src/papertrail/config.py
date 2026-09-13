"""Settings + corpus.yaml resolution — replaces both kb/config.py and
webapp/app/config.py (see the papertrail migration plan's Step 4).

Governing rule: nothing in this module (or anything downstream of it)
computes a path from `__file__`. A corpus's data lives wherever its
corpus.yaml lives — never inside the installed package — so this module's
only job is finding that directory and resolving every other path relative
to it, once, in a function whose result is passed around explicitly rather
than read off a module global.

Two behavior fixes over the code this replaces:
- kb/config.py hardcoded every path as `Path(__file__).parent`-relative
  with no env override at all, and `mkdir`'d 7 directories as an IMPORT
  side effect (every process that imported it, not just pipeline runs).
  Here, directory creation is opt-in (`create_dirs=True`) and paths always
  come from an explicit corpus root.
- webapp/app/config.py called `load_dotenv()` AFTER already reading
  `os.environ` for its path overrides, so path vars set only in `.env`
  (not truly exported) were silently ignored. Here `.env` loading always
  happens first, as a fixed step inside one function body — that ordering
  bug is structurally impossible to reintroduce by editing this file.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import find_dotenv, load_dotenv


class CorpusConfigError(Exception):
    """Raised when corpus.yaml can't be located, or is invalid."""


@dataclass(frozen=True)
class Corpus:
    """Parsed corpus.yaml. `raw` carries the full parsed YAML — nothing
    already written against corpus.yaml's existing keys is blocked on
    this refactor; `taxonomy`/`navigation` below are typed, validated
    views over raw["taxonomy"]/raw["navigation"] (papertrail migration
    Step 9, Seam 3)."""
    root: Path
    raw: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return (self.raw.get("corpus") or {}).get("name") or self.root.name

    @property
    def title(self) -> str:
        """Display title for branding (web app <title>/<h1>, MCP index page,
        etc.) — papertrail migration Step 9, Seam 4. Was hardcoded as
        "WASP-12b Knowledge Base" in five separate places; now one
        corpus.yaml value, falling back to a generic default for a corpus
        that doesn't set one."""
        return (self.raw.get("corpus") or {}).get("title") or f"{self.name} Knowledge Base"

    @property
    def glyph(self) -> str:
        return (self.raw.get("corpus") or {}).get("glyph") or ""

    @property
    def description(self) -> str:
        return (self.raw.get("corpus") or {}).get("description") or ""

    @property
    def taxonomy(self):
        from .taxonomy import load_taxonomy
        return load_taxonomy(self)

    @property
    def navigation(self):
        from .taxonomy import load_navigation
        return load_navigation(self, self.taxonomy, self.raw.get("wiki_headings") or [])

    def marker_dir(self) -> Path:
        raw = self.raw["source"]["marker_dir"]
        p = Path(raw)
        return p if p.is_absolute() else (self.root / p).resolve()

    def load_module(self, mod_path: str):
        """Import a module referenced from corpus.yaml and return the
        module object. Shared by adapter_module() (metadata_adapter) and
        papertrail.identity.load_citation_style() (identity.citation_style)
        — any corpus.yaml key naming a Python entry point uses this same
        resolution, so a corpus author only needs to learn it once.

        Two forms, distinguished by whether it looks like a path:
        - "./adapter.py" or "../somewhere/adapter.py" (corpus-relative file
          path) — loaded via importlib.util.spec_from_file_location, no
          sys.path mutation, no import-name collision possible. This is
          what a self-contained corpus directory uses (papertrail migration
          Step 8: corpus.yaml + adapter.py + data/ + wiki/ all under one root).
        - "kb.adapters.wasp12b" (dotted module path) — resolved via
          importlib.import_module after inserting the corpus root's PARENT
          directory onto sys.path, mirroring kb/config.py's original
          load_papers() exactly. This is wasp12b's interim value between
          migration Steps 6 and 8, when its adapter still lived inside the
          old kb/ layout rather than its own corpus directory."""
        if mod_path.startswith((".", "/")):
            file_path = Path(mod_path)
            if not file_path.is_absolute():
                file_path = (self.root / file_path).resolve()
            spec = importlib.util.spec_from_file_location(
                f"_corpus_module_{self.name}_{file_path.stem}", file_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        parent = str(self.root.parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)
        return importlib.import_module(mod_path)

    def adapter_module(self):
        """Import corpus.yaml's metadata_adapter module and return the
        module object — kb/build_index.py (Stage E) needs the module itself
        (for load_all_including_stubs(), an undeclared second entry point
        corpus.yaml doesn't mention — the papertrail migration's Step 9
        turns this into a proper declared entry point), while load_papers()
        below only needs the one function."""
        mod_path, _ = self.raw["source"]["metadata_adapter"].split(":")
        return self.load_module(mod_path)

    def documents_path(self) -> Path | None:
        """Path to a declarative document manifest, when configured.

        ``source.documents_file`` is the zero-code alternative to a Python
        ``metadata_adapter``.  Keeping it corpus-relative makes a small corpus
        self-contained while preserving adapters for richer metadata sources.
        """
        raw = (self.raw.get("source") or {}).get("documents_file")
        if not raw:
            return None
        path = Path(raw)
        return path if path.is_absolute() else (self.root / path).resolve()

    def load_papers(self) -> list[dict]:
        """One dict per fetchable/known document, each with at least
        doc_id/title/authors/year/journal/extra (docs/spec/01_data_contracts.md)."""
        source = self.raw.get("source") or {}
        documents_path = self.documents_path()
        adapter = source.get("metadata_adapter")
        if documents_path and adapter:
            raise CorpusConfigError(
                "source.documents_file and source.metadata_adapter are mutually exclusive")
        if documents_path:
            if not documents_path.exists():
                raise CorpusConfigError(f"documents file not found: {documents_path}")
            parsed = yaml.safe_load(documents_path.read_text(encoding="utf-8")) or {}
            documents = parsed.get("documents") if isinstance(parsed, dict) else parsed
            if not isinstance(documents, list):
                raise CorpusConfigError(
                    f"documents file must contain a list or a 'documents' list: {documents_path}")
            for i, document in enumerate(documents):
                if not isinstance(document, dict) or not document.get("doc_id"):
                    raise CorpusConfigError(
                        f"document {i} in {documents_path} must be a mapping with doc_id")
            return documents
        if not adapter:
            raise CorpusConfigError(
                "source must define either documents_file or metadata_adapter")
        _, func_name = adapter.split(":")
        return getattr(self.adapter_module(), func_name)()


@dataclass(frozen=True)
class Settings:
    corpus_root: Path
    data_dir: Path
    sections_dir: Path
    assets_dir: Path
    classification_dir: Path
    wiki_dir: Path
    sqlite_path: Path
    base_url: str
    api_key: str
    bulk_model: str
    synth_model: str
    chat_model: str
    pipeline_backend: str
    pipeline_codex_model: str
    agent_backend: str
    agent_model: str
    agent_mcp_url: str
    agent_timeout_seconds: float
    agent_recursion_limit: int
    codex_model: str
    codex_bin: str
    corpus: Corpus


def _find_corpus_root(explicit: str | Path | None) -> Path:
    """--corpus PATH -> PAPERTRAIL_CORPUS/KB_DIR env -> upward search from
    cwd for a corpus.yaml -> a hard, actionable error. No implicit fallback
    to a bundled/default corpus — a caller must say which one."""
    if explicit is not None:
        root = Path(explicit).resolve()
        if not (root / "corpus.yaml").exists():
            raise CorpusConfigError(f"no corpus.yaml found at {root}")
        return root

    env = os.environ.get("PAPERTRAIL_CORPUS") or os.environ.get("KB_DIR")
    if env:
        root = Path(env).resolve()
        if not (root / "corpus.yaml").exists():
            raise CorpusConfigError(
                f"no corpus.yaml found at {root} (from PAPERTRAIL_CORPUS/KB_DIR)")
        return root

    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "corpus.yaml").exists():
            return candidate

    raise CorpusConfigError(
        "couldn't locate a corpus.yaml — pass --corpus PATH, set "
        "PAPERTRAIL_CORPUS, or run from inside (or below) a corpus directory"
    )


def _env_path(name: str, default: Path) -> Path:
    """PAPERTRAIL_<NAME> wins over the bare legacy <NAME> (kept for one
    release per the migration plan), which wins over `default`."""
    val = os.environ.get(f"PAPERTRAIL_{name}") or os.environ.get(name)
    return Path(val).resolve() if val else default


def load_settings(corpus_root: str | Path | None = None, *, create_dirs: bool = False) -> Settings:
    root = _find_corpus_root(corpus_root)

    # .env, fully before any os.environ read below — see the module
    # docstring's second behavior fix. override=False: a genuinely exported
    # env var still beats .env, which is what a real deployment expects.
    load_dotenv(root / ".env", override=False)
    found = find_dotenv(usecwd=True)
    if found:
        load_dotenv(found, override=False)

    raw: dict = {}
    corpus_yaml = root / "corpus.yaml"
    if corpus_yaml.exists():
        raw = yaml.safe_load(corpus_yaml.read_text(encoding="utf-8")) or {}

    data_dir = _env_path("DATA_DIR", root / "data")
    sections_dir = _env_path("SECTIONS_DIR", data_dir / "sections")
    assets_dir = _env_path("ASSETS_DIR", data_dir / "assets")
    classification_dir = _env_path("CLASSIFICATION_DIR", data_dir / "classification")
    wiki_dir = _env_path("WIKI_DIR", root / "wiki")
    sqlite_path = _env_path("KB_SQLITE", data_dir / "kb.sqlite")

    if create_dirs:
        for d in (data_dir, sections_dir, assets_dir, classification_dir,
                  wiki_dir, wiki_dir / "papers", wiki_dir / "topics"):
            d.mkdir(parents=True, exist_ok=True)

    chat_model = os.getenv("CHAT_MODEL", "anthropic/claude-sonnet-5")
    agent_backend = os.getenv("AGENT_BACKEND", "langchain").strip().lower()
    if agent_backend not in {"langchain", "codex"}:
        raise CorpusConfigError(
            "AGENT_BACKEND must be either 'langchain' or 'codex'"
        )
    pipeline_backend = os.getenv("PIPELINE_BACKEND", "openai").strip().lower()
    if pipeline_backend not in {"openai", "codex"}:
        raise CorpusConfigError(
            "PIPELINE_BACKEND must be either 'openai' or 'codex'"
        )
    codex_model = os.getenv("CODEX_MODEL", "")
    return Settings(
        corpus_root=root,
        data_dir=data_dir,
        sections_dir=sections_dir,
        assets_dir=assets_dir,
        classification_dir=classification_dir,
        wiki_dir=wiki_dir,
        sqlite_path=sqlite_path,
        base_url=os.getenv("BASE_URL", ""),
        api_key=os.getenv("API_KEY", ""),
        bulk_model=os.getenv("BULK_MODEL", "deepseek/deepseek-v4-flash-0731"),
        synth_model=os.getenv("SYNTH_MODEL", "anthropic/claude-opus-4.8"),
        chat_model=chat_model,
        pipeline_backend=pipeline_backend,
        pipeline_codex_model=os.getenv("PIPELINE_CODEX_MODEL", "") or codex_model,
        agent_backend=agent_backend,
        agent_model=os.getenv("AGENT_MODEL", chat_model),
        agent_mcp_url=os.getenv("AGENT_MCP_URL", "http://127.0.0.1:8013/mcp"),
        agent_timeout_seconds=float(os.getenv("AGENT_TIMEOUT_SECONDS", "120")),
        agent_recursion_limit=int(os.getenv("AGENT_RECURSION_LIMIT", "30")),
        codex_model=codex_model,
        codex_bin=os.getenv("CODEX_BIN", ""),
        corpus=Corpus(root=root, raw=raw),
    )
