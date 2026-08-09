"""Stage A (kb/build_sections.py) golden tests.

build_tree() is fully deterministic and I/O-free except for
extract_image_asset's write-to-disk side effect on Figure blocks — every
test here redirects kb.config.ASSETS_DIR to a tmp_path first so nothing
touches the real corpus data. This is a regression harness for the
Marker-JSON -> section-tree pipeline stage ahead of the papertrail
extraction: any refactor of build_sections.py must reproduce these three
fixtures byte-for-byte (see the migration plan's Step 6 verification).
"""
import json
from pathlib import Path

import pytest

from kb import build_sections as bs
from kb import config

FIXTURES = Path(__file__).parent / "fixtures"

CASES = ["simple", "listgroup_li", "listgroup_br"]


@pytest.fixture(autouse=True)
def _redirect_assets_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ASSETS_DIR", tmp_path)


@pytest.mark.parametrize("name", CASES)
def test_build_tree_matches_golden(name):
    data = json.loads((FIXTURES / "marker" / f"{name}.json").read_text())
    doc = data["json"]
    structure = bs.build_tree(doc, name)
    expected = json.loads((FIXTURES / "expected" / f"{name}.json").read_text())
    assert structure == expected


def test_figure_image_written_relative_to_assets_dir(tmp_path):
    """extract_image_asset decodes the Figure block's base64 payload to
    ASSETS_DIR/<doc_id>/<slug>.jpeg and returns that path relative to
    ASSETS_DIR — verified against the actual bytes, not just the path
    string, since the golden JSON test above only checks the path."""
    data = json.loads((FIXTURES / "marker" / "simple.json").read_text())
    doc = data["json"]
    structure = bs.build_tree(doc, "simple")
    fig = next(n for n in structure[0]["nodes"] if n["block_type"] == "figure")
    assert fig["image_asset"] == "simple/page_0_figure_0.jpeg"
    written = tmp_path / fig["image_asset"]
    assert written.exists()
    assert written.read_bytes() == b"fake-image-bytes"


def test_reference_nodes_use_shared_source_block_id():
    """Both listgroup fixtures split one ListGroup block into N reference
    leaves — verifies the ListGroup <li>-parsing fix documented in the
    module docstring (kasi's reference extractor drops bibliographies
    entirely here; this must not regress)."""
    data = json.loads((FIXTURES / "marker" / "listgroup_li.json").read_text())
    structure = bs.build_tree(data["json"], "listgroup_li")
    refs = structure[0]["nodes"]
    assert len(refs) == 2
    assert all(r["block_type"] == "reference" for r in refs)
    assert [r["list_index"] for r in refs] == [0, 1]
    assert all(r["source_block_ids"] == ["/page/0/listgroup/0"] for r in refs)


def test_unknown_block_type_recurses_defensively():
    """A block type not in RECURSE_TYPES/DROP_TYPES and not otherwise
    handled (e.g. TextInlineMath from the local marker lib, never emitted
    by the hosted API but defended against) must recurse into its children
    rather than silently dropping content."""
    data = json.loads((FIXTURES / "marker" / "listgroup_br.json").read_text())
    structure = bs.build_tree(data["json"], "listgroup_br")
    refs_container = structure[1]["nodes"]
    texts = [n["text"] for n in refs_container if n["block_type"] == "paragraph"]
    assert "Recursed-into unknown block type." in texts


def test_dropped_block_types_produce_no_nodes():
    """TableOfContents/Picture (DROP_TYPES) must contribute zero nodes."""
    data = json.loads((FIXTURES / "marker" / "listgroup_br.json").read_text())
    structure = bs.build_tree(data["json"], "listgroup_br")
    all_text = json.dumps(structure)
    assert "ignored" not in all_text  # the TableOfContents block's html
