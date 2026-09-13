"""Stage A (papertrail.pipeline.sections) golden tests.

build_tree() is fully deterministic and I/O-free except for
extract_image_asset's write-to-disk side effect on Figure blocks — every
test here passes a tmp_path as `assets_dir` explicitly (build_tree's third,
optional parameter — no module-global ASSETS_DIR exists anymore post-Step-4)
so nothing touches the real corpus data. This is a regression harness for
the Marker-JSON -> section-tree pipeline stage: any refactor of
pipeline/sections.py must reproduce these three fixtures byte-for-byte (see
the migration plan's Step 6 verification).
"""
import json
from pathlib import Path

import pytest

from papertrail.pipeline import sections as bs

FIXTURES = Path(__file__).parent / "fixtures"

CASES = ["simple", "listgroup_li", "listgroup_br"]


@pytest.mark.parametrize("name", CASES)
def test_build_tree_matches_golden(name, tmp_path):
    data = json.loads((FIXTURES / "marker" / f"{name}.json").read_text())
    doc = data["json"]
    structure = bs.build_tree(doc, name, tmp_path)
    expected = json.loads((FIXTURES / "expected" / f"{name}.json").read_text())
    assert structure == expected


def test_figure_image_written_relative_to_assets_dir(tmp_path):
    """extract_image_asset decodes the Figure block's base64 payload to
    assets_dir/<doc_id>/<slug>.jpeg and returns that path relative to
    assets_dir — verified against the actual bytes, not just the path
    string, since the golden JSON test above only checks the path."""
    data = json.loads((FIXTURES / "marker" / "simple.json").read_text())
    doc = data["json"]
    structure = bs.build_tree(doc, "simple", tmp_path)
    fig = next(n for n in structure[0]["nodes"] if n["block_type"] == "figure")
    assert fig["image_asset"] == "simple/page_0_figure_0.jpeg"
    written = tmp_path / fig["image_asset"]
    assert written.exists()
    assert written.read_bytes() == b"fake-image-bytes"


def test_reference_nodes_use_shared_source_block_id(tmp_path):
    """Both listgroup fixtures split one ListGroup block into N reference
    leaves — verifies the ListGroup <li>-parsing fix documented in the
    module docstring (kasi's reference extractor drops bibliographies
    entirely here; this must not regress)."""
    data = json.loads((FIXTURES / "marker" / "listgroup_li.json").read_text())
    structure = bs.build_tree(data["json"], "listgroup_li", tmp_path)
    refs = structure[0]["nodes"]
    assert len(refs) == 2
    assert all(r["block_type"] == "reference" for r in refs)
    assert [r["list_index"] for r in refs] == [0, 1]
    assert all(r["source_block_ids"] == ["/page/0/listgroup/0"] for r in refs)


def test_unknown_block_type_recurses_defensively(tmp_path):
    """A block type not in RECURSE_TYPES/DROP_TYPES and not otherwise
    handled (e.g. TextInlineMath from the local marker lib, never emitted
    by the hosted API but defended against) must recurse into its children
    rather than silently dropping content."""
    data = json.loads((FIXTURES / "marker" / "listgroup_br.json").read_text())
    structure = bs.build_tree(data["json"], "listgroup_br", tmp_path)
    refs_container = structure[1]["nodes"]
    texts = [n["text"] for n in refs_container if n["block_type"] == "paragraph"]
    assert "Recursed-into unknown block type." in texts


def test_dropped_block_types_produce_no_nodes(tmp_path):
    """TableOfContents/Picture (DROP_TYPES) must contribute zero nodes."""
    data = json.loads((FIXTURES / "marker" / "listgroup_br.json").read_text())
    structure = bs.build_tree(data["json"], "listgroup_br", tmp_path)
    all_text = json.dumps(structure)
    assert "ignored" not in all_text  # the TableOfContents block's html


def test_caption_after_figure_is_attached_to_that_figure(tmp_path):
    doc = {"children": [{
        "block_type": "Page",
        "children": [
            {
                "id": "/page/6/Figure/1", "block_type": "Figure", "page": 6,
                "html": '<img alt="Figure 7">', "images": {},
            },
            {
                "id": "/page/6/Caption/2", "block_type": "Caption", "page": 6,
                "html": "<p><b>Figure 7.</b> <math>K_s</math>-band light curve.</p>",
            },
            {
                "id": "/page/6/Text/3", "block_type": "Text", "page": 6,
                "html": "<p>Following discussion.</p>",
            },
        ],
    }]}

    structure = bs.build_tree(doc, "paper", tmp_path)

    assert len(structure) == 3
    assert structure[0]["block_type"] == "figure"
    assert structure[0]["text"] == ""
    assert structure[0]["source_block_ids"] == ["/page/6/Figure/1"]
    assert structure[1] == {
        "node_id": "0002",
        "kind": "leaf",
        "block_type": "caption",
        "caption_for": "0001",
        "text": "**Figure 7.** $K_s$-band light curve.",
        "page": 6,
        "source_block_ids": ["/page/6/Caption/2"],
    }
    assert structure[2]["text"] == "Following discussion."
