"""webapp/app/answer.py characterization tests.

validate_and_enrich() is the single shared citation-validation path (chat,
kb/mcp_server.py's validate_answer/publish_answer, POST /api/answers — see
CLAUDE.md's citations section). These tests pin its two distinct checks
(hard citation-resolution vs. the soft uncited-author-year-mention
heuristic) so the papertrail extraction's Seam 1 (identity/citation style)
can be checked against this exact behavior for the wasp12b corpus.
"""
import pytest

from app.answer import (
    InvalidCitationsError,
    find_uncited_author_year_mentions,
    validate_and_enrich,
)
from app.registry import Doc


def make_doc(doc_id, has_fulltext=True, node_ids=("0001",), authors="Smith, John",
             year=2020, journal="ApJ"):
    node_by_id = {nid: {"block_type": "paragraph", "page": 1, "text": "x"} for nid in node_ids}
    return Doc(doc_id=doc_id, doc_name=f"Paper {doc_id}", label=doc_id,
               has_fulltext=has_fulltext, authors=authors, year=year, journal=journal,
               node_by_id=node_by_id)


def test_resolving_citation_enriches_and_has_no_warnings():
    docs = {"D1": make_doc("D1")}
    payload = {"answer": "A claim [1].",
               "citations": [{"id": 1, "doc_id": "D1", "node_id": "0001"}]}
    result = validate_and_enrich(payload, docs)
    assert result["warnings"] == []
    assert result["documents"] == ["D1"]
    cite = result["results"][0]["citations"][0]
    assert cite["doc_id"] == "D1" and cite["node_id"] == "0001"
    assert cite["author_year"] == "Smith 2020"


def test_unknown_doc_id_is_offending_and_raises_in_strict_mode():
    docs = {"D1": make_doc("D1")}
    payload = {"answer": "A claim [1].",
               "citations": [{"id": 1, "doc_id": "NOPE", "node_id": "0001"}]}
    with pytest.raises(InvalidCitationsError) as exc:
        validate_and_enrich(payload, docs, strict=True)
    assert exc.value.offending[0]["reason"] == "unknown doc_id"


def test_metadata_only_doc_is_rejected_even_if_doc_id_known():
    """A citation to a has_fulltext=False stub doc must be rejected — there
    is no real node content behind it (docs/spec/01_data_contracts.md)."""
    docs = {"D1": make_doc("D1", has_fulltext=False)}
    payload = {"answer": "A claim [1].",
               "citations": [{"id": 1, "doc_id": "D1", "node_id": "0001"}]}
    with pytest.raises(InvalidCitationsError) as exc:
        validate_and_enrich(payload, docs, strict=True)
    assert exc.value.offending[0]["reason"] == "doc has no full text (metadata-only)"


def test_unknown_node_id_is_offending():
    docs = {"D1": make_doc("D1")}
    payload = {"answer": "A claim [1].",
               "citations": [{"id": 1, "doc_id": "D1", "node_id": "9999"}]}
    with pytest.raises(InvalidCitationsError) as exc:
        validate_and_enrich(payload, docs, strict=True)
    assert exc.value.offending[0]["reason"] == "unknown node_id"


def test_non_strict_mode_drops_offending_citations_as_warning_not_exception():
    docs = {"D1": make_doc("D1")}
    payload = {"answer": "A claim [1].",
               "citations": [{"id": 1, "doc_id": "NOPE", "node_id": "0001"}]}
    result = validate_and_enrich(payload, docs, strict=False)
    assert result["results"][0]["citations"] == []
    assert any("dropped 1 invalid citation" in w for w in result["warnings"])


def test_missing_and_unused_marker_warnings():
    docs = {"D1": make_doc("D1", node_ids=("0001", "0002"))}
    payload = {"answer": "Claims [1] and [2].",
               "citations": [{"id": 1, "doc_id": "D1", "node_id": "0001"}]}
    result = validate_and_enrich(payload, docs, strict=False)
    warnings = " ".join(result["warnings"])
    assert "marker(s) [2] have no citation" in warnings


@pytest.mark.parametrize("text,expect_hit", [
    ("Madhusudhan et al. (2011) reported a carbon-rich atmosphere, in contrast to the host star.", True),
    ("Madhusudhan et al. (2011) reported a carbon-rich atmosphere [2], in contrast to the host star [2].", False),
    ("Yee et al. (2020, 2021a) found orbital decay.", True),
    ("No author-year mention at all here.", False),
])
def test_find_uncited_author_year_mentions(text, expect_hit):
    hits = find_uncited_author_year_mentions(text)
    assert bool(hits) == expect_hit


def test_uncited_mention_heuristic_respects_window():
    """A [n] marker far past the 200-char default window still counts as
    uncited — this is the documented, tunable false-positive tradeoff
    (webapp/app/answer.py's window docstring), not a bug to fix here."""
    filler = "x" * 250
    text = f"Smith (2020) reported a result. {filler} [1]"
    assert find_uncited_author_year_mentions(text)
    assert not find_uncited_author_year_mentions(text, window=10_000)
