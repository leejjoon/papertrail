"""Corpus registry: kb.sqlite-driven document tree + lazy per-doc rendering.

Adapted from multi_doc_navigator/app/registry.py. Two real differences from
that reference:

1. **No manifest file.** The document tree is generated from kb.sqlite's
   `docs` table (grouped by research_theme — a paper can appear under
   multiple theme groups, since that facet is multi-valued; harmless, the
   same doc_id under two branches works fine as leaves with no shared state).
2. **Lazy, cached rendering, not import-time.** The navigator renders every
   document's HTML at load (fine for 14 papers); at 190+20 docs here,
   `CorpusRegistry.docs` is a lazy mapping — a `Doc` is built and cached on
   first access, and `reload()` drops the cache without restarting the
   process (a paper page an agent republishes should show up in the *next*
   request, not require a server restart).

Node model differs too: nodes are container (heading, no body text) or leaf
(paragraph/table/figure/equation/footnote/reference — see docs/spec/01_data_
contracts.md), not the navigator's single flat {node_id, title, line_num,
text} shape. `iter_nodes`/`build_toc_outline`/citation lookups are adapted
accordingly, but the *contract* — parent/child text disjoint, node_id is a
stable per-doc anchor — is identical, so `citations.py`'s validate_and_enrich
needed only cosmetic changes.

Tree-walking helpers (iter_nodes, find_node_with_siblings, context_window,
build_toc_outline, ...) live in core/nodes.py, not here — split out in the
papertrail extraction's Step 3 so this module and core/renderer.py can both
depend on them without a cycle between registry and renderer.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Iterator

from . import _legacy_config as config
from .nodes import _flatten_by_id, build_toc_outline
from .renderer import render_doc_html


@dataclass
class Doc:
    doc_id: str
    doc_name: str
    label: str
    has_fulltext: bool
    authors: str = ""
    year: int | None = None
    journal: str = ""
    node_by_id: dict[str, dict] = field(repr=False, default_factory=dict)
    html: str = field(repr=False, default="")
    tree: dict = field(repr=False, default_factory=dict)

    @property
    def node_count(self) -> int:
        return len(self.node_by_id)


def derive_label(doc_id: str, year: int | None) -> str:
    """Short chip/reference label. doc_ids here are bibcodes (YYYYJJJJVVVPPPPA
    format) or a slugified title for the 3 no-bibcode entries — a bibcode's
    year is already the first 4 chars, so just tag on the record's own year
    field for the no-bibcode case."""
    if doc_id[:4].isdigit():
        return doc_id[:4]
    return str(year) if year else doc_id


def author_year_label(authors: str, year: int | None) -> str:
    """'Lastname YYYY' / 'Lastname1 & Lastname2 YYYY' / 'Lastname1 et al.
    YYYY' from the ADS-style 'Last, First; Last, First; ...' authors string —
    the traditional-citation label used for a reference entry, as opposed to
    derive_label's bare-year chip label. Matches the convention already used
    in agent-published answers (e.g. answers/wasp12b_co_ratio_controversy.md)."""
    names = [a.split(",")[0].strip() for a in (authors or "").split(";") if a.strip()]
    y = str(year) if year else ""
    if not names:
        return y
    if len(names) == 1:
        return f"{names[0]} {y}".strip()
    if len(names) == 2:
        return f"{names[0]} & {names[1]} {y}".strip()
    return f"{names[0]} et al. {y}".strip()


def ads_url(doc_id: str) -> str | None:
    """ADS abstract-page link for a bibcode doc_id, None for the 3 no-bibcode
    (slugified-title) stub entries. doc_id is safe_bibcode()'d ('&' -> '+'
    for filesystem safety); ADS itself expects '&' URL-encoded as '%26'."""
    if doc_id[:4].isdigit():
        return f"https://ui.adsabs.harvard.edu/abs/{doc_id.replace('+', '%26')}"
    return None


class CorpusRegistry:
    """Lazy, cached view over kb.sqlite + kb/data/sections/*.json."""

    def __init__(self):
        self._doc_cache: dict[str, Doc] = {}
        self._doc_tree: dict | None = None
        self._meta_by_id: dict[str, sqlite3.Row] = {}
        self.docs: Mapping[str, Doc] = _LazyDocMap(self)
        self._load_meta()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{config.KB_SQLITE}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _load_meta(self) -> None:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM docs ORDER BY year, doc_id").fetchall()
        finally:
            conn.close()
        self._meta_by_id = {r["doc_id"]: r for r in rows}

    def reload(self) -> None:
        """Drop all caches and re-read kb.sqlite — call after the pipeline
        adds/updates papers, no process restart needed."""
        self._doc_cache.clear()
        self._doc_tree = None
        self._load_meta()

    def _build_doc(self, doc_id: str) -> Doc:
        row = self._meta_by_id[doc_id]
        authors = json.loads(row["authors_json"]) if row["authors_json"] else ""
        if not row["has_fulltext"]:
            return Doc(doc_id=doc_id, doc_name=row["title"] or doc_id,
                       label=derive_label(doc_id, row["year"]), has_fulltext=False,
                       authors=authors, year=row["year"], journal=row["journal"] or "")
        path = config.SECTIONS_DIR / f"{doc_id}.json"
        tree = json.loads(path.read_text(encoding="utf-8"))
        return Doc(
            doc_id=doc_id,
            doc_name=row["title"] or tree.get("doc_name") or doc_id,
            label=derive_label(doc_id, row["year"]),
            has_fulltext=True,
            authors=authors, year=row["year"], journal=row["journal"] or "",
            node_by_id=_flatten_by_id(tree["structure"]),
            html=render_doc_html(tree["structure"]),
            tree=tree,
        )

    def get_doc(self, doc_id: str) -> Doc:
        if doc_id not in self._meta_by_id:
            raise KeyError(doc_id)
        if doc_id not in self._doc_cache:
            self._doc_cache[doc_id] = self._build_doc(doc_id)
        return self._doc_cache[doc_id]

    @property
    def doc_tree(self) -> dict:
        if self._doc_tree is None:
            self._doc_tree = self._build_doc_tree()
        return self._doc_tree

    def _build_doc_tree(self) -> dict:
        theme_groups: dict[str, list[dict]] = {}
        stubs: list[dict] = []
        for doc_id, row in self._meta_by_id.items():
            leaf = {"doc_id": doc_id, "doc_name": row["title"] or doc_id,
                    "label": derive_label(doc_id, row["year"]),
                    "year": row["year"], "has_fulltext": bool(row["has_fulltext"])}
            if not row["has_fulltext"]:
                stubs.append(leaf)
                continue
            themes = json.loads(row["research_theme_json"] or "[]")
            for theme in themes or ["(uncategorized)"]:
                theme_groups.setdefault(theme, []).append(leaf)

        children = []
        for theme in sorted(theme_groups, key=lambda t: -len(theme_groups[t])):
            members = sorted(theme_groups[theme], key=lambda d: (d["year"] or 0, d["doc_id"]))
            children.append({"title": theme, "children": members})
        if stubs:
            children.append({"title": "Metadata only (no full text)",
                             "children": sorted(stubs, key=lambda d: (d["year"] or 0, d["doc_id"]))})
        return {"title": "WASP-12b Knowledge Base", "children": children}

    def all_doc_ids(self) -> list[str]:
        return list(self._meta_by_id.keys())


class _LazyDocMap(Mapping):
    """Dict-like facade so `registry.docs[doc_id]` / `.get(doc_id)` work
    exactly like the navigator's `DOCS` dict, but build on demand."""

    def __init__(self, registry: CorpusRegistry):
        self._registry = registry

    def __getitem__(self, doc_id: str) -> Doc:
        return self._registry.get_doc(doc_id)

    def get(self, doc_id: str, default=None):
        try:
            return self._registry.get_doc(doc_id)
        except KeyError:
            return default

    def __iter__(self) -> Iterator[str]:
        return iter(self._registry.all_doc_ids())

    def __len__(self) -> int:
        return len(self._registry.all_doc_ids())


def section_payload(doc: Doc, node_id: str) -> dict[str, Any]:
    """{node_id, title, block_type, text, children} for a single section —
    used by GET /api/documents/{doc_id}/sections/{node_id}."""
    node = doc.node_by_id[node_id]
    if node["kind"] == "container":
        return {"node_id": node["node_id"], "title": node["title"], "block_type": None,
                "text": "", "children": build_toc_outline(node["nodes"])}
    return {"node_id": node["node_id"], "title": node.get("_parent_title") or "",
            "block_type": node["block_type"], "text": node["text"], "children": []}
