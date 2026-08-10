"""webapp/app/registry.py's context-escalation helpers.

find_node_with_siblings / concat_subtree / context_window are pure, I/O-free
functions shared by kb/mcp_server.py's get_section(context=N) and
webapp/app/chat.py's need_context escalation (see CLAUDE.md's citations
section). Pinned here so the papertrail extraction can move them into
core/nodes.py and prove byte-identical behavior against a synthetic tree
rather than needing the real corpus loaded.
"""
from papertrail.core.nodes import concat_subtree, context_window, find_node_with_siblings

STRUCTURE = [
    {"node_id": "h1", "kind": "container", "title": "Intro", "nodes": [
        {"node_id": "p1", "kind": "leaf", "block_type": "paragraph", "text": "First para."},
        {"node_id": "p2", "kind": "leaf", "block_type": "paragraph", "text": "This issue is discussed further below."},
        {"node_id": "p3", "kind": "leaf", "block_type": "paragraph", "text": "Third para."},
    ]},
    {"node_id": "h2", "kind": "container", "title": "Methods", "nodes": [
        {"node_id": "p4", "kind": "leaf", "block_type": "paragraph", "text": "Fourth para."},
    ]},
]


def test_find_node_with_siblings_leaf():
    node, parent_title, siblings, idx = find_node_with_siblings(STRUCTURE, "p2")
    assert node["node_id"] == "p2"
    assert parent_title == "Intro"
    assert idx == 1
    assert [s["node_id"] for s in siblings] == ["p1", "p2", "p3"]


def test_find_node_with_siblings_top_level_container():
    node, parent_title, siblings, idx = find_node_with_siblings(STRUCTURE, "h2")
    assert node["node_id"] == "h2"
    assert parent_title is None
    assert idx == 1
    assert [s["node_id"] for s in siblings] == ["h1", "h2"]


def test_find_node_with_siblings_not_found():
    assert find_node_with_siblings(STRUCTURE, "nope") == (None, None, None, None)


def test_concat_subtree_container():
    text = concat_subtree(STRUCTURE[0])
    assert text.startswith("## Intro")
    assert "First para." in text and "Third para." in text


def test_context_window_zero_is_leaf_text_prefixed_with_parent():
    """context<=0 (the default) must reproduce the original, pre-escalation
    get_section leaf format exactly — this is the byte-identity guarantee
    the escalation feature was built to preserve."""
    assert context_window(STRUCTURE, "p2", 0) == "[Intro] This issue is discussed further below."
    assert context_window(STRUCTURE, "p2") == context_window(STRUCTURE, "p2", 0)


def test_context_window_expands_within_section_and_brackets_cited_node():
    text = context_window(STRUCTURE, "p2", context=1)
    assert text.startswith("[Intro] ")
    assert ">>> CITED NODE p2 >>>" in text
    assert "First para." in text and "Third para." in text
    assert "<<<" in text


def test_context_window_clamps_at_section_boundary():
    """Sibling expansion must not cross into an unrelated subsection — p1 is
    the first child of h1, so context=5 around it must not pull in h2's p4."""
    text = context_window(STRUCTURE, "p1", context=5)
    assert "Fourth para." not in text
    assert "First para." in text and "Third para." in text


def test_context_window_container_sibling_contributes_title_only():
    """A container node adjacent to the cited leaf must show only its
    heading, not its full subtree prose (a boundary marker, not more text
    to read) — exercised here via the top-level structure where h1/h2 are
    siblings of each other, not of a leaf, so use a nested case instead."""
    nested = [
        {"node_id": "h1", "kind": "container", "title": "Top", "nodes": [
            {"node_id": "sub", "kind": "container", "title": "Sub Section", "nodes": [
                {"node_id": "deep", "kind": "leaf", "block_type": "paragraph", "text": "Deep text."},
            ]},
            {"node_id": "leaf1", "kind": "leaf", "block_type": "paragraph", "text": "Leaf one."},
        ]},
    ]
    text = context_window(nested, "leaf1", context=1)
    assert "## Sub Section" in text
    assert "Deep text." not in text


def test_context_window_missing_node_returns_none():
    assert context_window(STRUCTURE, "nope", 2) is None
