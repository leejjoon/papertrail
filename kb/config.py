"""
kb configuration — loads LLM-gateway credentials from ../.env, defines paths,
and parses corpus.yaml. Mirrors paper_download/config.py's shape.

Never log or print API keys.
"""
import importlib
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # so "kb.adapters.<name>" resolves regardless
                                    # of how a kb/*.py script was invoked
load_dotenv(ROOT / ".env")

BASE_URL: str = os.getenv("BASE_URL", "")
API_KEY: str = os.getenv("API_KEY", "")
BULK_MODEL: str = os.getenv("BULK_MODEL", "deepseek/deepseek-v4-flash-0731")
SYNTH_MODEL: str = os.getenv("SYNTH_MODEL", "anthropic/claude-opus-4.8")
CHAT_MODEL: str = os.getenv("CHAT_MODEL", "anthropic/claude-sonnet-5")

KB_DIR = Path(__file__).parent
CORPUS_YAML = KB_DIR / "corpus.yaml"
DATA_DIR = KB_DIR / "data"
SECTIONS_DIR = DATA_DIR / "sections"
ASSETS_DIR = DATA_DIR / "assets"
CLASSIFICATION_DIR = DATA_DIR / "classification"
KB_SQLITE = DATA_DIR / "kb.sqlite"
WIKI_DIR = KB_DIR / "wiki"
WIKI_PAPERS_DIR = WIKI_DIR / "papers"
WIKI_TOPICS_DIR = WIKI_DIR / "topics"

for _d in (DATA_DIR, SECTIONS_DIR, ASSETS_DIR, CLASSIFICATION_DIR,
           WIKI_DIR, WIKI_PAPERS_DIR, WIKI_TOPICS_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # read-only mount (e.g. an MCP server container) — dirs already exist


def load_corpus_config() -> dict:
    """Parse kb/corpus.yaml."""
    return yaml.safe_load(CORPUS_YAML.read_text(encoding="utf-8"))


def _resolve_marker_dir(cfg: dict) -> Path:
    raw = cfg["source"]["marker_dir"]
    p = Path(raw)
    return p if p.is_absolute() else (KB_DIR / p).resolve()


def marker_dir() -> Path:
    return _resolve_marker_dir(load_corpus_config())


def load_papers() -> list[dict]:
    """Dynamically import corpus.yaml's metadata_adapter ("module.path:func") and
    call it. Returns one dict per fetchable/known document, each with at least
    doc_id/title/authors/year/journal/extra (see docs/spec/01_data_contracts.md)."""
    cfg = load_corpus_config()
    mod_path, func_name = cfg["source"]["metadata_adapter"].split(":")
    module = importlib.import_module(mod_path)
    loader = getattr(module, func_name)
    return loader()


def safe_bibcode(bibcode: str) -> str:
    """Filesystem-safe bibcode: '&' (A&A) -> '+'. '+' never occurs in bibcodes."""
    return bibcode.replace("&", "+")


def slugify(value: str) -> str:
    """Single slug rule for topic-page filenames and cross-links."""
    import re
    slug = value.strip().lower().replace("/", "-")
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")
