"""Section-tree renderer: one HTML string per node, in document order.

Adapted from multi_doc_navigator/app/renderer.py. That version rendered a
single flat node shape and rewrote Obsidian-style ``![[..]]`` figure
wikilinks; this corpus has no such rewrite step (Stage A already writes plain
``image_asset`` relative paths) but does have real per-block-type rendering:
paragraphs go through python-markdown (bold/italic/literal ``<sup>/<sub>``
HTML/``$..$`` math all pass through untouched — KaTeX auto-render, loaded
client-side, finds and typesets the math after the page loads), tables keep
their raw HTML, figures render an ``<img>`` from the extracted asset, equations
get a dedicated display-math wrapper, and container (heading) nodes render
just their title with no body — the parent/child text-disjoint invariant this
whole system relies on for citations.
"""
from __future__ import annotations

import html as html_stdlib

import markdown

MD_EXTENSIONS = ["extra", "sane_lists", "smarty"]


def _anchor_wrap(node_id: str, css_class: str, inner: str) -> str:
    anchor = f'<a id="node-{node_id}" class="node-anchor"></a>'
    return f'{anchor}\n<section class="node-block {css_class}" data-node-id="{node_id}">{inner}</section>'


def render_node_html(node: dict, asset_prefix: str = "assets") -> str:
    node_id = node["node_id"]

    if node["kind"] == "container":
        level = min(max(node.get("level", 2), 1), 6)
        title = html_stdlib.escape(node["title"])
        return _anchor_wrap(node_id, "node-heading", f"<h{level}>{title}</h{level}>")

    bt = node["block_type"]
    text = node.get("text", "")

    if bt == "paragraph" or bt == "footnote" or bt == "reference" or bt == "code":
        md = markdown.Markdown(extensions=MD_EXTENSIONS)  # fresh instance per
                                                             # node — python-markdown
                                                             # is stateful (footnote
                                                             # ids leak otherwise)
        body = md.convert(text)
        css = {"footnote": "node-footnote", "reference": "node-reference",
               "code": "node-code"}.get(bt, "node-paragraph")
        return _anchor_wrap(node_id, css, body)

    if bt == "equation":
        # Rendered as display math; text is already "$$\n<latex>\n$$".
        return _anchor_wrap(node_id, "node-equation", f'<div class="equation">{text}</div>')

    if bt == "table":
        caption = node.get("caption")
        cap_html = f'<figcaption>{_inline_md(caption)}</figcaption>' if caption else ""
        return _anchor_wrap(node_id, "node-table", f"<figure>{text}{cap_html}</figure>")

    if bt == "figure":
        caption = node.get("caption") or text
        cap_html = f'<figcaption>{_inline_md(caption)}</figcaption>' if caption else ""
        asset = node.get("image_asset")
        img_html = f'<img src="{asset_prefix}/{asset}" alt="">' if asset else \
                   '<div class="figure-missing">(no image extracted)</div>'
        return _anchor_wrap(node_id, "node-figure", f"<figure>{img_html}{cap_html}</figure>")

    if bt == "stub":
        return _anchor_wrap(node_id, "node-stub", f"<p>{html_stdlib.escape(text)}</p>")

    # Unknown block_type — render as plain escaped text rather than drop it.
    return _anchor_wrap(node_id, "node-unknown", f"<p>{html_stdlib.escape(text)}</p>")


def _inline_md(text: str) -> str:
    md = markdown.Markdown(extensions=["smarty"])
    out = md.convert(text)
    # Markdown wraps single-line input in <p>...</p>; captions read better inline.
    return out[3:-4] if out.startswith("<p>") and out.endswith("</p>") else out


def render_doc_html(structure: list[dict], asset_prefix: str = "assets") -> str:
    """Render an entire document from its section tree, in document order."""
    from .registry import iter_nodes  # local import avoids a circular dependency

    parts = [render_node_html(node, asset_prefix) for node in iter_nodes(structure)]
    return "\n".join(parts)
