"""Runtime configuration for the WASP-12b KB web app.

Corpus-location env vars mirror kb/config.py so both the pipeline and the web
app agree on where things live without duplicating a path-computation scheme;
override any of them to point this app at a different corpus (see
docs/spec/05_adoption.md), independent of the kb/ pipeline's own working dir.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

KB_DIR = Path(os.environ.get("KB_DIR", ROOT / "kb")).resolve()
KB_SQLITE = Path(os.environ.get("KB_SQLITE", KB_DIR / "data" / "kb.sqlite")).resolve()
SECTIONS_DIR = Path(os.environ.get("SECTIONS_DIR", KB_DIR / "data" / "sections")).resolve()
ASSETS_DIR = Path(os.environ.get("ASSETS_DIR", KB_DIR / "data" / "assets")).resolve()
WIKI_DIR = Path(os.environ.get("WIKI_DIR", KB_DIR / "wiki")).resolve()

# LLM gateway for chat — same env vars as kb/config.py, loaded independently
# since the web app may run as a separate process/deployment from the pipeline.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
BASE_URL: str = os.getenv("BASE_URL", "")
API_KEY: str = os.getenv("API_KEY", "")
CHAT_MODEL: str = os.getenv("CHAT_MODEL", "anthropic/claude-sonnet-5")
BULK_MODEL: str = os.getenv("BULK_MODEL", "deepseek/deepseek-v4-flash-0731")  # query rewrite fallback
