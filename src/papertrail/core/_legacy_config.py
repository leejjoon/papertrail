"""Temporary compatibility shim — migration Step 3 only.

The moved core/*.py modules (registry, store, search) need path/model
config, but papertrail.config doesn't exist yet (that's Step 4). Rather
than re-implement path resolution twice, re-export webapp/app/config.py's
already-computed values so this step is a pure move with zero behavior
change. Delete this file once Step 4 lands and every core/*.py imports
papertrail.config directly.
"""
import sys
from pathlib import Path

# webapp/app/main.py is normally launched as `python -m uvicorn
# webapp.app.main:app` from the repo root, which puts the repo root on
# sys.path automatically — but kb/mcp_server.py (still `python
# kb/mcp_server.py` pre-Step-5) only puts kb/'s parent on sys.path, and
# webapp/ has no __init__.py (namespace package), so make sure the repo
# root is present either way before importing webapp.app.config.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from webapp.app import config as _cfg  # noqa: E402

KB_SQLITE = _cfg.KB_SQLITE
SECTIONS_DIR = _cfg.SECTIONS_DIR
ASSETS_DIR = _cfg.ASSETS_DIR
WIKI_DIR = _cfg.WIKI_DIR
BASE_URL = _cfg.BASE_URL
API_KEY = _cfg.API_KEY
CHAT_MODEL = _cfg.CHAT_MODEL
BULK_MODEL = _cfg.BULK_MODEL
