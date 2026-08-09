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

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import find_dotenv, load_dotenv


class CorpusConfigError(Exception):
    """Raised when corpus.yaml can't be located, or is invalid."""


@dataclass(frozen=True)
class Corpus:
    """Parsed corpus.yaml. Minimal for now — just enough for path
    resolution (Step 4). Taxonomy/identity/prompt/branding semantics land
    as typed fields in the papertrail migration's Step 9 (the four
    genericity seams); until then, `raw` carries the full parsed YAML so
    nothing already written against corpus.yaml's existing keys
    (research_theme, wiki_headings, ...) is blocked on this refactor."""
    root: Path
    raw: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return (self.raw.get("corpus") or {}).get("name") or self.root.name


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
        chat_model=os.getenv("CHAT_MODEL", "anthropic/claude-sonnet-5"),
        corpus=Corpus(root=root, raw=raw),
    )
