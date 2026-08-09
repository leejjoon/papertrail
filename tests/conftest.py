"""Shared test setup.

These tests run against the PRE-MOVE code layout (kb/*.py, webapp/app/*.py) —
they exist to characterize current behavior so the papertrail extraction
(docs/spec/, .claude/plans/) can be verified step by step. Once code moves
into src/papertrail/, these imports get updated in place rather than
duplicated — see the migration plan's Step 1 note.

Both `kb/*.py` and `webapp/app/*.py` assume being importable as `kb.<mod>` /
`app.<mod>` from a sys.path that includes the repo root / webapp/
respectively (the same hack every pipeline stage and kb/mcp_server.py do at
their own top). Do it once here instead of per test file.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
for p in (ROOT, ROOT / "webapp"):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)
