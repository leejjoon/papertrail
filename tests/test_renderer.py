from papertrail.core.renderer import render_doc_html, render_node_html


def test_paragraph_markdown_does_not_consume_latex_underscores():
    node = {
        "node_id": "0075",
        "kind": "leaf",
        "block_type": "paragraph",
        "text": (
            "**Figure 7.** $K_s$ light curve. Blueshifted $\\text{H}_2$ emission. "
            "Stars mark $V \\equiv v_{\\text{jet}} \\cos i$."
        ),
    }

    rendered = render_node_html(node)

    assert "<strong>Figure 7.</strong>" in rendered
    assert "$K_s$" in rendered
    assert "$\\text{H}_2$" in rendered
    assert "$V \\equiv v_{\\text{jet}} \\cos i$" in rendered
    assert "<em" not in rendered


def test_following_caption_renders_inside_figure_and_keeps_both_node_ids():
    structure = [
        {
            "node_id": "0074",
            "kind": "leaf",
            "block_type": "figure",
            "text": "",
            "image_asset": "paper/page_6_Figure_1.jpeg",
        },
        {
            "node_id": "0075",
            "kind": "leaf",
            "block_type": "caption",
            "caption_for": "0074",
            "text": "**Figure 7.** $K_s$ light curve.",
        },
    ]

    rendered = render_doc_html(structure)

    assert rendered.count("<figure>") == 1
    assert '<section class="node-block node-figure" data-node-id="0074">' in rendered
    assert '<figcaption class="node-block node-caption" data-node-id="0075">' in rendered
    assert '<a id="node-0075" class="node-anchor"></a>' in rendered
    assert "<strong>Figure 7.</strong> $K_s$ light curve." in rendered
