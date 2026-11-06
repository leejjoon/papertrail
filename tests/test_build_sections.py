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


def _text(block_id, page, html):
    return {"id": block_id, "block_type": "Text", "page": page, "html": html}


def _two_pages(first_page, second_page):
    return {"children": [
        {"block_type": "Page", "children": first_page},
        {"block_type": "Page", "children": second_page},
    ]}


def test_paragraph_split_across_pages_is_merged(tmp_path):
    """Marker flags a paragraph cut by a page break with
    class='has-continuation'; the next Text block (past any page header or
    footer) joins it in one node citing both source blocks."""
    doc = _two_pages(
        [_text("/page/0/Text/1", 0,
               "<p block-type='Text' class='has-continuation'>The bands trace the</p>"),
         {"id": "/page/0/PageFooter/2", "block_type": "PageFooter", "page": 0,
          "html": "<p>Page 1</p>"}],
        [{"id": "/page/1/PageHeader/0", "block_type": "PageHeader", "page": 1,
          "html": "<p>Running head</p>"},
         _text("/page/1/Text/1", 1, "<p block-type='Text'>ionisation state.</p>"),
         _text("/page/1/Text/2", 1, "<p block-type='Text'>Next paragraph.</p>")],
    )
    structure = bs.build_tree(doc, "paper", tmp_path)
    assert [n["text"] for n in structure] == [
        "The bands trace the ionisation state.", "Next paragraph."]
    assert structure[0]["page"] == 0
    assert structure[0]["source_block_ids"] == ["/page/0/Text/1", "/page/1/Text/1"]
    assert [n["node_id"] for n in structure] == ["0001", "0002"]


def test_hyphenated_word_split_across_pages_is_rejoined(tmp_path):
    doc = _two_pages(
        [_text("/page/0/Text/0", 0,
               "<p class='has-continuation'>a photo-</p>")],
        [_text("/page/1/Text/0", 1, "<p>dissociation front</p>")],
    )
    structure = bs.build_tree(doc, "paper", tmp_path)
    assert [n["text"] for n in structure] == ["a photodissociation front"]


def test_continuation_chains_across_three_blocks(tmp_path):
    doc = {"children": [{"block_type": "Page", "children": [
        _text("/page/0/Text/0", 0, "<p class='has-continuation'>one</p>"),
        _text("/page/0/Text/1", 0, "<p class='has-continuation'>two</p>"),
        _text("/page/0/Text/2", 0, "<p>three</p>"),
    ]}]}
    structure = bs.build_tree(doc, "paper", tmp_path)
    assert [n["text"] for n in structure] == ["one two three"]
    assert len(structure[0]["source_block_ids"]) == 3


def test_continuation_is_dropped_when_another_block_intervenes(tmp_path):
    """Marker only flags a continuation when the very next non-header/footer
    block is text; if anything else sits between, the paragraphs stay apart."""
    doc = _two_pages(
        [_text("/page/0/Text/0", 0, "<p class='has-continuation'>Before</p>")],
        [{"id": "/page/1/Equation/0", "block_type": "Equation", "page": 1,
          "html": "<math>x=1</math>"},
         _text("/page/1/Text/1", 1, "<p>after</p>")],
    )
    structure = bs.build_tree(doc, "paper", tmp_path)
    assert [n["block_type"] for n in structure] == ["paragraph", "equation", "paragraph"]
    assert structure[2]["text"] == "after"


def test_trailing_hyphen_kept_unless_after_lowercase_letter():
    """Only lowercase-letter + hyphen is treated as a broken word."""
    assert bs.join_continued("a photo-", "dissociation") == "a photodissociation"
    assert bs.join_continued("the H-", "band") == "the H- band"
    assert bs.join_continued("from 20-", "30 K") == "from 20- 30 K"


# Geometric fallback: Datalab's hosted API strips has-continuation, so splits
# are recognised from bboxes. Page 1000 x 1400, two columns at x 50-480 / 520-950.
PAGE_BBOX = [0, 0, 1000, 1400]


def _gtext(block_id, page, bbox, text):
    return {"id": block_id, "block_type": "Text", "page": page,
            "bbox": bbox, "html": f"<p>{text}</p>"}


def _gpage(*children):
    return {"block_type": "Page", "bbox": PAGE_BBOX, "children": list(children)}


def test_unflagged_split_across_page_break_is_merged(tmp_path):
    doc = {"children": [
        _gpage(_gtext("/page/2/Text/15", 2, [520, 1200, 950, 1350],
                      "allows the model to replace DFT")),
        _gpage({"id": "/page/3/PageHeader/0", "block_type": "PageHeader", "page": 3,
                "bbox": [50, 40, 300, 60], "html": "<p>Mai et al.</p>"},
               _gtext("/page/3/Text/1", 3, [50, 80, 480, 200],
                      "electronic structure calculations.")),
    ]}
    structure = bs.build_tree(doc, "paper", tmp_path)
    assert [n["text"] for n in structure] == [
        "allows the model to replace DFT electronic structure calculations."]
    assert structure[0]["source_block_ids"] == ["/page/2/Text/15", "/page/3/Text/1"]


def test_unflagged_split_across_column_break_is_merged(tmp_path):
    doc = {"children": [_gpage(
        _gtext("/page/1/Text/6", 1, [50, 900, 480, 1350],
               "faced challenges related to accuracy and"),
        _gtext("/page/1/Text/7", 1, [520, 100, 950, 400],
               "transferability of force fields."),
    )]}
    structure = bs.build_tree(doc, "paper", tmp_path)
    assert [n["text"] for n in structure] == [
        "faced challenges related to accuracy and transferability of force fields."]


def test_finished_sentence_at_column_break_is_not_merged(tmp_path):
    doc = {"children": [_gpage(
        _gtext("/page/1/Text/6", 1, [50, 900, 480, 1350], "The paragraph ends here."),
        _gtext("/page/1/Text/7", 1, [520, 100, 950, 400], "A new paragraph starts."),
    )]}
    structure = bs.build_tree(doc, "paper", tmp_path)
    assert len(structure) == 2


def test_consecutive_blocks_in_same_column_are_not_merged(tmp_path):
    """Front-matter lines such as affiliations end without punctuation but are
    stacked in one column: no page or column break, so no merge."""
    doc = {"children": [_gpage(
        _gtext("/page/0/Text/8", 0, [50, 300, 480, 330], "1 Guangxi University, China"),
        _gtext("/page/0/Text/9", 0, [50, 335, 480, 365], "2 TU Wien, 1060 Vienna, Austria"),
    )]}
    structure = bs.build_tree(doc, "paper", tmp_path)
    assert len(structure) == 2


def test_ends_mid_sentence_ignores_trailing_marks():
    assert bs.ends_mid_sentence("accuracy and")
    assert not bs.ends_mid_sentence("as shown (Smith 2007).")
    assert not bs.ends_mid_sentence("the cross-section (Berens & Wilson 1981):")
    assert not bs.ends_mid_sentence("data bases.<sup>2</sup>")
    assert not bs.ends_mid_sentence("*a full sentence.*")


def test_list_items_split_on_br_inside_single_li():
    html = ('<ul style="list-style-type: none"><li>Andrews, H. 2015, <i>ApJ</i>, 807, 99'
            '<br/> Bakes, E. L. O. 2001, <i>ApJ</i>, 556, 501<br/> Tielens 2008</li></ul>')
    assert bs.parse_list_items(html) == [
        "Andrews, H. 2015, *ApJ*, 807, 99", "Bakes, E. L. O. 2001, *ApJ*, 556, 501",
        "Tielens 2008"]


def test_list_items_from_sibling_paragraphs():
    html = ("<p>Roser, J. E., &amp; Ricca, A. 2015, <i>ApJ</i>, 801, 108</p>"
            "<p>Roser, J. E., &amp; Ricca, A. 2020, <i>IAUS</i>, 15, 415</p>")
    assert bs.parse_list_items(html) == [
        "Roser, J. E., & Ricca, A. 2015, *ApJ*, 801, 108",
        "Roser, J. E., & Ricca, A. 2020, *IAUS*, 15, 415"]
