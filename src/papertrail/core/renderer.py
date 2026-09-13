"""Section-tree renderer: one HTML string per node, in document order.

Adapted from multi_doc_navigator/app/renderer.py. That version rendered a
single flat node shape and rewrote Obsidian-style ``![[..]]`` figure
wikilinks; this corpus has no such rewrite step (Stage A already writes plain
``image_asset`` relative paths) but does have real per-block-type rendering:
paragraphs go through python-markdown (bold/italic/literal ``<sup>/<sub>``
HTML; ``$..$`` math is shielded from Markdown and restored for KaTeX), tables keep
their raw HTML, figures render an ``<img>`` from the extracted asset, equations
get a dedicated display-math wrapper, and container (heading) nodes render
just their title with no body — the parent/child text-disjoint invariant this
whole system relies on for citations.
"""
from __future__ import annotations

import html as html_stdlib
import re

import markdown

from .nodes import iter_nodes

MD_EXTENSIONS = ["extra", "sane_lists", "smarty"]
MATH_SPAN_RE = re.compile(
    r"(?<!\\)(\$\$[\s\S]*?(?<!\\)\$\$|\$(?:\\.|[^$\n\\])*(?<!\\)\$)"
)


def _anchor_wrap(node_id: str, css_class: str, inner: str) -> str:
    anchor = f'<a id="node-{node_id}" class="node-anchor"></a>'
    return f'{anchor}\n<section class="node-block {css_class}" data-node-id="{node_id}">{inner}</section>'


def _markdown_with_protected_math(text: str, extensions: list[str]) -> str:
    """Render Markdown without treating LaTeX underscores as emphasis."""
    spans: list[str] = []

    def protect(match: re.Match) -> str:
        spans.append(match.group(0))
        return f"PAPERTRAILMATHSPANZX{len(spans) - 1}XZ"

    protected = MATH_SPAN_RE.sub(protect, text)
    rendered = markdown.Markdown(extensions=extensions).convert(protected)
    for index, span in enumerate(spans):
        token = f"PAPERTRAILMATHSPANZX{index}XZ"
        rendered = rendered.replace(token, html_stdlib.escape(span, quote=False))
    return rendered


def render_node_html(node: dict, asset_prefix: str = "assets") -> str:
    node_id = node["node_id"]

    if node["kind"] == "container":
        level = min(max(node.get("level", 2), 1), 6)
        title = html_stdlib.escape(node["title"])
        return _anchor_wrap(node_id, "node-heading", f"<h{level}>{title}</h{level}>")

    bt = node["block_type"]
    text = node.get("text", "")

    if bt == "paragraph" or bt == "footnote" or bt == "reference" or bt == "code":
        body = _markdown_with_protected_math(text, MD_EXTENSIONS)
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

    if bt == "caption":
        return _anchor_wrap(node_id, "node-caption", f"<p>{_inline_md(text)}</p>")

    if bt == "stub":
        return _anchor_wrap(node_id, "node-stub", f"<p>{html_stdlib.escape(text)}</p>")

    # Unknown block_type — render as plain escaped text rather than drop it.
    return _anchor_wrap(node_id, "node-unknown", f"<p>{html_stdlib.escape(text)}</p>")


def _inline_md(text: str) -> str:
    out = _markdown_with_protected_math(text, ["smarty"])
    # Markdown wraps single-line input in <p>...</p>; captions read better inline.
    return out[3:-4] if out.startswith("<p>") and out.endswith("</p>") else out


def render_doc_html(structure: list[dict], asset_prefix: str = "assets") -> str:
    """Render an entire document from its section tree, in document order."""
    nodes = list(iter_nodes(structure))
    parts = []
    index = 0
    while index < len(nodes):
        node = nodes[index]
        following = nodes[index + 1] if index + 1 < len(nodes) else None
        if (node.get("block_type") in {"figure", "table"}
                and following is not None
                and following.get("block_type") == "caption"
                and following.get("caption_for") == node["node_id"]):
            caption_id = following["node_id"]
            caption_html = (
                f'<a id="node-{caption_id}" class="node-anchor"></a>'
                f'<figcaption class="node-block node-caption" '
                f'data-node-id="{caption_id}">{_inline_md(following["text"])}</figcaption>'
            )
            if node["block_type"] == "figure":
                asset = node.get("image_asset")
                media_html = (f'<img src="{asset_prefix}/{asset}" alt="">' if asset else
                              '<div class="figure-missing">(no image extracted)</div>')
            else:
                media_html = node.get("text", "")
            parts.append(_anchor_wrap(
                node["node_id"], f'node-{node["block_type"]}',
                f"<figure>{media_html}{caption_html}</figure>",
            ))
            index += 2
            continue
        parts.append(render_node_html(node, asset_prefix))
        index += 1
    return "\n".join(parts)
