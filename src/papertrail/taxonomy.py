"""Taxonomy/navigation config: which facets exist, how each is presented,
which ones get topic pages, which facet groups the doc tree, and which
wiki headings feed a topic-page digest.

Before this module, all of that was wasp12b-specific Python literals
scattered across pipeline/topic_pages.py (DIGEST_SECTIONS, the hardcoded
theme/method/facility triple, the facet->slug-kind mapping, the "## By
theme/method/facility" index sections), pipeline/paper_pages.py (hardcoded
similarity weights), core/registry.py (_TREE_FACET), and mcp/server.py
(hardcoded facet params on search_fulltext/list_papers). Every taxonomy
fact now lives in corpus.yaml; the code just reads it.

load_taxonomy()/load_navigation() validate EAGERLY (at load time, not at
first use) so a misconfigured corpus.yaml fails with one clear message
instead of a confusing downstream symptom — this is exactly the class of
bug the migration plan calls out: before, renaming a wiki heading without
updating topic_pages.py's DIGEST_SECTIONS constant made every topic
synthesis fail with "no member paper has a wiki page yet," which doesn't
even mention headings.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class TaxonomyConfigError(Exception):
    """Raised when corpus.yaml's taxonomy/navigation block is invalid."""


@dataclass(frozen=True)
class FacetSpec:
    name: str
    multi: bool
    values: list[str]
    label: str
    topic_pages: bool
    slug_prefix: str
    similarity_weight: int
    description: str = ""


@dataclass(frozen=True)
class Navigation:
    tree_facet: str
    table_columns: list[str] = field(default_factory=list)
    year_pages: bool = True
    digest_headings: list[str] = field(default_factory=list)


def _heading_names(wiki_headings: list) -> list[str]:
    """wiki_headings entries are either a bare string or {name, ...} —
    normalize to just the names (see corpus.yaml's structured-heading
    support for prompt guidance, papertrail migration Seam 2)."""
    return [h if isinstance(h, str) else h["name"] for h in wiki_headings]


def load_taxonomy(corpus) -> dict[str, FacetSpec]:
    raw = (corpus.raw.get("taxonomy") or {})
    specs = {}
    for name, spec in raw.items():
        specs[name] = FacetSpec(
            name=name,
            multi=bool(spec.get("multi", False)),
            values=list(spec.get("values") or []),
            label=spec.get("label") or name.replace("_", " ").title(),
            topic_pages=bool(spec.get("topic_pages", True)),
            slug_prefix=spec.get("slug_prefix") or name,
            similarity_weight=int(spec.get("similarity_weight", 1)),
            description=spec.get("description", ""),
        )

    # Eager validation: unique slug_prefix among topic-page facets, so two
    # facets can't silently collide into the same output filename.
    prefixes = [s.slug_prefix for s in specs.values() if s.topic_pages]
    dupes = {p for p in prefixes if prefixes.count(p) > 1}
    if dupes:
        raise TaxonomyConfigError(f"duplicate slug_prefix among topic_pages facets: {dupes}")

    return specs


def load_navigation(corpus, taxonomy: dict[str, FacetSpec], wiki_headings: list) -> Navigation:
    raw = (corpus.raw.get("navigation") or {})
    heading_names = _heading_names(wiki_headings)

    tree_facet = raw.get("tree_facet")
    if tree_facet is None and taxonomy:
        tree_facet = next(iter(taxonomy))  # first declared facet, a reasonable default
    if tree_facet is not None and tree_facet not in taxonomy:
        raise TaxonomyConfigError(f"navigation.tree_facet {tree_facet!r} is not a declared taxonomy facet")

    table_columns = raw.get("table_columns") or []
    unknown_cols = [c for c in table_columns if c not in taxonomy]
    if unknown_cols:
        raise TaxonomyConfigError(f"navigation.table_columns references unknown facet(s): {unknown_cols}")

    digest_headings = raw.get("digest_headings")
    if digest_headings is None:
        digest_headings = heading_names[:1]  # default: just the first heading
    unknown_headings = [h for h in digest_headings if h not in heading_names]
    if unknown_headings:
        raise TaxonomyConfigError(
            f"navigation.digest_headings references heading(s) not in wiki_headings: {unknown_headings}")

    return Navigation(tree_facet=tree_facet or "", table_columns=list(table_columns),
                      year_pages=bool(raw.get("year_pages", True)),
                      digest_headings=list(digest_headings))


def topic_page_facets(taxonomy: dict[str, FacetSpec]) -> list[FacetSpec]:
    return [s for s in taxonomy.values() if s.topic_pages]
