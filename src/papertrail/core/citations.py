"""Validate + enrich a supplied answer payload.

Adapted from multi_doc_navigator/app/answer.py — the exact same seam, shared
by POST /api/answers, kb/mcp_server.py's validate_answer/publish_answer, and
(in the chat flow) the RAG endpoint's own citations before persisting. No
model calls; pure lookups against the registry.

Two additions over the navigator's version, per docs/spec/01_data_contracts.md:
- a citation's doc must have has_fulltext=True (citing a metadata-only stub
  doc is always an error — there's no real node content to point at);
- the enriched citation carries `block_type` and `page` (this corpus's nodes
  don't have a line_num; page is more meaningful for a scientific paper
  anyway) instead of the navigator's `line_num`.

Identity/citation-style fields (papertrail migration Step 9, Seam 1): the
enriched citation's "reference_label"/"external_links" (and the uncited-
mention heuristic's pattern/window) now come from a CitationStyle passed in
by the caller, rather than hardcoded ADS/author-year functions — the same
logic, just parameterized. `reference_label`/`external_links` REPLACE the
old `author_year`/`ads_url` keys; see webapp/static/index.html's `extLinks()`
helper for the frontend's backward-compat shim over already-stored payloads
that still carry the old keys.
"""
from __future__ import annotations

import re
from typing import Any

from ..identity import CitationStyle
from .registry import Doc

_MARKER_RE = re.compile(r"\[(\d+)\]")


def find_uncited_author_year_mentions(text: str, style: CitationStyle, window: int | None = None) -> list[str]:
    """Author-year mentions with no [n] marker within `window` chars after
    them — each one is very likely an uncited or too-loosely-cited claim.
    This is an approximate, tunable signal, not a hard rule, and it's
    entirely style-dependent: a style with no mention_pattern() (e.g.
    PlainStyle) makes this a no-op, since the heuristic has never been
    calibrated against that style's prose conventions. For AuthorYearStyle,
    the model's natural prose puts one citation at the end of a full
    descriptive sentence (not immediately after the author name), and
    legitimate citations have been observed 100-345 chars after their
    mention — 200 (AuthorYearStyle.mention_window()) is a middle ground
    between catching genuinely disconnected citations and not flagging
    every long-but-correctly-cited compound sentence."""
    pattern = style.mention_pattern()
    if pattern is None:
        return []
    win = style.mention_window() if window is None else window
    hits = []
    for m in pattern.finditer(text or ""):
        tail = text[m.end():m.end() + win]
        if not _MARKER_RE.search(tail):
            hits.append(m.group(0))
    return hits


class InvalidCitationsError(Exception):
    def __init__(self, offending: list[dict]):
        self.offending = offending
        super().__init__(f"{len(offending)} invalid citation(s)")


def normalize_payload(payload: Any) -> dict:
    if isinstance(payload, dict) and "results" in payload:
        return payload
    if isinstance(payload, dict) and "answer" in payload:
        return {"results": [payload]}
    raise ValueError("payload must be a result object or {results: [...]}")


def _markers_in(answer: str) -> set[int]:
    return {int(n) for n in _MARKER_RE.findall(answer or "")}


def _section_title(node: dict) -> str:
    """A container's own title, or a leaf's parent heading — never a leaf's
    own (nonexistent) title, matching kb/mcp_server.py's get_section
    convention: a citation display always shows orienting context."""
    if node.get("kind") == "container":
        return node.get("title", "")
    return node.get("_parent_title") or ""


def validate_and_enrich(payload: Any, docs: dict[str, Doc], style: CitationStyle,
                        strict: bool = True) -> dict:
    data = normalize_payload(payload)
    results_in = data.get("results") or []

    offending: list[dict] = []
    warnings: list[str] = []
    out_results: list[dict] = []
    doc_union: list[str] = []

    for idx, result in enumerate(results_in):
        answer = result.get("answer", "")
        label = result.get("label") or result.get("question") or f"Result {idx + 1}"
        enriched: list[dict] = []
        cited_ids: set[int] = set()

        for cite in result.get("citations", []) or []:
            doc_id = cite.get("doc_id")
            node_id = cite.get("node_id")
            doc = docs.get(doc_id)
            node = doc.node_by_id.get(node_id) if (doc and doc.has_fulltext) else None
            if doc is None or not doc.has_fulltext:
                offending.append({"result_index": idx, "id": cite.get("id"),
                                  "doc_id": doc_id, "node_id": node_id,
                                  "reason": "unknown doc_id" if doc is None
                                  else "doc has no full text (metadata-only)"})
                continue
            if node is None:
                offending.append({"result_index": idx, "id": cite.get("id"),
                                  "doc_id": doc_id, "node_id": node_id,
                                  "reason": "unknown node_id"})
                continue
            cited_ids.add(cite.get("id"))
            if doc_id not in doc_union:
                doc_union.append(doc_id)
            enriched.append({
                "id": cite.get("id"), "doc_id": doc_id, "node_id": node_id,
                "doc_name": doc.doc_name, "section_title": _section_title(node),
                "block_type": node.get("block_type"), "page": node.get("page"),
                # Traditional-citation display fields — a bare "[n] 2011 §
                # some heading" tells a reader nothing about which paper it
                # is; reference_label/journal/external_links let the UI show
                # something like "Madhusudhan et al. 2011, A high C/O
                # ratio... (Nature)" with an ADS/DOI link where available.
                "reference_label": style.reference_label(doc.ref()),
                "journal": doc.journal,
                "external_links": [{"label": l.label, "url": l.url}
                                   for l in style.external_links(doc.ref())],
            })

        markers = _markers_in(answer)
        missing_citation = markers - cited_ids
        unused_citation = cited_ids - markers
        if missing_citation:
            warnings.append(f"result {idx}: marker(s) {sorted(missing_citation)} have no citation")
        if unused_citation:
            warnings.append(f"result {idx}: citation id(s) {sorted(unused_citation)} not cited in answer")
        uncited_mentions = find_uncited_author_year_mentions(answer, style)
        if uncited_mentions:
            warnings.append(f"result {idx}: possible uncited claim(s) near: {uncited_mentions}")

        out_results.append({"label": label, "question": result.get("question"),
                            "answer": answer, "citations": enriched})

    if offending:
        if strict:
            raise InvalidCitationsError(offending)
        warnings.append(f"dropped {len(offending)} invalid citation(s)")

    return {"results": out_results, "documents": doc_union, "warnings": warnings}
