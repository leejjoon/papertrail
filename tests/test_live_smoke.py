"""Live smoke test against the running webapp (see CLAUDE.md's "Running
things" section — no fixed port convention, 8009 is what's running now).

Skipped entirely if nothing answers on the configured port, so this suite
stays green in an environment where the server isn't up (CI, a fresh
clone) while still being the fast, no-mocking check to run by hand before
and after each migration step (see the migration plan's per-step
"Verify" sections, which diff these same four endpoints against
.migration_baselines/).
"""
import json
import os
import urllib.error
import urllib.request

import pytest

PORT = os.environ.get("WEBAPP_TEST_PORT", "8009")
BASE = f"http://127.0.0.1:{PORT}"


def _get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=3) as resp:
        return resp.status, json.loads(resp.read())


def _server_up():
    try:
        _get("/api/health")
        return True
    except (urllib.error.URLError, ConnectionError, OSError):
        return False


pytestmark = pytest.mark.skipif(not _server_up(), reason=f"no webapp listening on {BASE}")


def test_health():
    status, body = _get("/api/health")
    assert status == 200
    assert body["status"] == "ok"
    assert body["doc_count"] > 0


def test_documents_tree():
    status, body = _get("/api/documents")
    assert status == 200
    assert "children" in body


def test_one_document():
    status, body = _get("/api/documents/2013Icar..225..432S")
    assert status == 200
    assert body["doc_id"] == "2013Icar..225..432S"


def test_search():
    status, body = _get("/api/search?q=orbital+decay")
    assert status == 200
    assert body["query"] == "orbital decay"
    assert len(body["hits"]) > 0
