"""Section-tree walkers: pure, I/O-free functions over the node model
(container/leaf — see docs/spec/01_data_contracts.md), shared by
core/registry.py, core/renderer.py, and the MCP server's get_section tool.
Split out of registry.py in the
papertrail extraction's Step 3 so core/renderer.py can import iter_nodes
without a circular dependency on core/registry.py (it previously did a
function-local `from .registry import iter_nodes` specifically to dodge
that cycle — no longer necessary now that neither module owns the walkers).
"""
from __future__ import annotations

from collections.abc import Iterator


def iter_nodes(structure: list[dict]) -> Iterator[dict]:
    """Yield every node depth-first, in document order (already the list
    order Stage A wrote — no line_num to sort by, unlike the navigator)."""
    for node in structure:
        yield node
        if node["kind"] == "container":
            yield from iter_nodes(node["nodes"])


def iter_leaves(structure: list[dict]) -> list[dict]:
    """Every leaf node (paragraph/table/figure/caption/equation/footnote/reference),
    depth-first, containers themselves excluded. <- kb/paper_pages.py's
    flatten_leaves, used by both Stage C (build_node_pool) and Stage D
    (node_text_for) — moved here so pipeline stages don't import from each
    other for a pure tree-walking helper."""
    out = []
    for n in structure:
        if n["kind"] == "container":
            out.extend(iter_leaves(n["nodes"]))
        else:
            out.append(n)
    return out


def _flatten_by_id(structure: list[dict], parent_title: str | None = None) -> dict[str, dict]:
    """Flat {node_id: node}. Leaf nodes get a synthetic '_parent_title' (their
    nearest enclosing heading) so a citation to one paragraph can still show
    orienting context, matching the MCP server's get_section convention."""
    out: dict[str, dict] = {}
    for node in structure:
        if node["kind"] == "container":
            out[node["node_id"]] = node
            out.update(_flatten_by_id(node["nodes"], node["title"]))
        else:
            out[node["node_id"]] = {**node, "_parent_title": parent_title}
    return out


def find_node_with_siblings(structure: list[dict], node_id: str, parent_title: str | None = None):
    """Depth-first lookup returning (node, parent_title, siblings, index),
    where `siblings` is the list `node` actually lives in (its parent's
    `nodes`, or the top-level structure) and `index` its position there —
    lets a caller build a window of neighboring nodes (see context_window).
    Returns (None, None, None, None) if node_id isn't found. Pure/I/O-free:
    used by the MCP server's get_section (external and browser-agent tool
    calling), with identical sibling-window semantics for both clients."""
    for i, n in enumerate(structure):
        if n["node_id"] == node_id:
            return n, parent_title, structure, i
        if n["kind"] == "container":
            found = find_node_with_siblings(n["nodes"], node_id, n["title"])
            if found[0] is not None:
                return found
    return None, None, None, None


def concat_subtree(node: dict) -> str:
    """A container node's own title plus every descendant's text,
    concatenated in document order — what a heading node_id resolves to,
    since there's no single 'the node's text' for a subtree."""
    if node["kind"] == "leaf":
        return node["text"]
    parts = [f"## {node['title']}"]
    for child in node["nodes"]:
        parts.append(concat_subtree(child))
    return "\n\n".join(parts)


def context_window(structure: list[dict], node_id: str, context: int = 0) -> str | None:
    """Read one node, optionally widened with `context` sibling nodes before
    and after it (same section only — expansion is capped at the immediate
    parent's children so it can't wander into an unrelated subsection). The
    cited node itself is bracketed (`>>> CITED NODE <id> >>> ... <<<`) so a
    reader can't mistake a neighbor for what's actually being cited. A
    container sibling contributes only its heading title (a boundary marker,
    not more prose to read). context<=0 (the default) reproduces the
    original, pre-escalation get_section leaf format exactly: just the node's
    own text, prefixed with its parent heading for orientation. Returns None
    if node_id isn't found in `structure`.

    Exists because a single paragraph read alone is often ambiguous — a
    pronoun/comparative pointing outside itself, or a paper-specific term
    with no definition in that one node — and validate_and_enrich() only
    proves a citation resolves, never that its text was read in enough
    context to support the claim next to it. See CLAUDE.md's citations
    section and .claude/skills/wasp12b-answer/SKILL.md step 2."""
    node, parent_title, siblings, idx = find_node_with_siblings(structure, node_id)
    if node is None:
        return None
    if node["kind"] == "container":
        return concat_subtree(node)
    prefix = f"[{parent_title}] " if parent_title else ""
    if context <= 0:
        return prefix + node["text"]
    lo, hi = max(0, idx - context), min(len(siblings), idx + context + 1)
    parts = []
    for i in range(lo, hi):
        sib = siblings[i]
        text = f"## {sib['title']}" if sib["kind"] == "container" else sib["text"]
        parts.append(f">>> CITED NODE {node_id} >>>\n{text}\n<<<" if i == idx else text)
    return prefix + "\n\n".join(parts)


def build_toc_outline(structure: list[dict]) -> list[dict]:
    """Nested {node_id, title, children} outline of CONTAINER nodes only — a
    table of contents lists headings, not every paragraph."""
    out = []
    for node in structure:
        if node["kind"] != "container":
            continue
        out.append({
            "node_id": node["node_id"],
            "title": node["title"],
            "children": build_toc_outline(node["nodes"]),
        })
    return out
