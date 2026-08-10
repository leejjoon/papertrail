"""Citation style: how a doc_id becomes a display label, a traditional
reference-list entry, external links, and how the uncited-author-year-
mention heuristic works — the astronomy/ADS-specific pieces that used to be
hardcoded across webapp/app/registry.py (derive_label/author_year_label/
ads_url), webapp/app/answer.py (_AUTHOR_YEAR_RE), kb/paper_pages.py (the
ADS/DOI footer), and kb/mcp_server.py (canonical_doc_id's '&'->'+' mangling).
Collapsed here in the papertrail migration's Step 9 (Seam 1).

A corpus with plain filename doc_ids and no bibcodes/authors (the minimal
adapter 05_adoption.md describes) gets sensible defaults from AuthorYearStyle
with no `identity:` config at all: `label` falls back to doc_id, `reference_label`
falls back to a title snippet, and `external_links` is empty (no link templates
configured, nothing rendered).
"""
from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote


@dataclass(frozen=True)
class DocRef:
    """Superset of the adapter contract ({doc_id, title, authors, year,
    journal, extra}) and the `docs` table row — constructible from either,
    so registry.py/citations.py/paper_pages.py's footer all work off one
    shape instead of three."""
    doc_id: str
    native_id: str | None = None
    title: str = ""
    authors: str | list[str] = ""
    year: int | None = None
    journal: str = ""
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExternalLink:
    label: str
    url: str


class CitationStyle(Protocol):
    def label(self, ref: DocRef) -> str: ...
    def reference_label(self, ref: DocRef) -> str: ...
    def external_links(self, ref: DocRef) -> list[ExternalLink]: ...
    def canonicalize(self, raw: str) -> list[str]: ...
    def mention_pattern(self) -> re.Pattern | None: ...
    def mention_window(self) -> int: ...


class _PercentEncode(str):
    """A str that percent-encodes itself on the default `{}` format spec,
    but passes through verbatim for `{var:raw}` — lets one external-link
    template work for both a bare identifier that needs URL-encoding
    (`{native_id}` -> '%26' for a literal '&') and a field that's already a
    full URL or has its own internal structure worth preserving
    (`{doi:raw}` — a DOI's own '/' shouldn't become '%2F'). The default
    safe set keeps '.', '-', '_', '~', ':' literal (bibcode/DOI-shaped
    identifiers are full of dots and dashes that must NOT be encoded —
    verified this reproduces the exact pre-Step-9 ADS URL, which escaped
    only '&', nothing else)."""
    def __format__(self, spec: str) -> str:
        if spec == "raw":
            return str(self)
        return quote(str(self), safe="-._~:")


def _link_namespace(ref: DocRef) -> dict:
    ns = {"doc_id": ref.doc_id, "native_id": ref.native_id or "",
          "year": ref.year if ref.year is not None else "",
          "title": ref.title, "journal": ref.journal}
    ns.update(ref.extra or {})
    return {k: _PercentEncode(v) if v is not None else _PercentEncode("") for k, v in ns.items()}


def _template_fields(template: str) -> list[str]:
    return [name.split(".")[0].split("[")[0]
            for _, name, _, _ in string.Formatter().parse(template) if name]


def _render_external_links(specs: list[dict], ref: DocRef) -> list[ExternalLink]:
    """A link whose template references a missing/empty variable is
    silently dropped — this is the entire "no bibcode -> no ADS link" rule,
    no `when:` mini-language needed. Checked by inspecting which fields the
    template actually references (via string.Formatter, so `{doi:raw}`'s
    format spec doesn't interfere), not by pattern-matching the rendered
    URL string."""
    ns = _link_namespace(ref)
    out = []
    for spec in specs:
        template = spec["url"]
        fields = _template_fields(template)
        if any(not ns.get(f) for f in fields):
            continue  # a referenced variable is missing or empty
        try:
            url = template.format_map(ns)
        except KeyError:
            continue
        out.append(ExternalLink(label=spec["label"], url=url))
    return out


class _BaseCitationStyle:
    def __init__(self, external_link_specs: list[dict] | None = None):
        self._link_specs = external_link_specs or []

    def external_links(self, ref: DocRef) -> list[ExternalLink]:
        return _render_external_links(self._link_specs, ref)

    def canonicalize(self, raw: str) -> list[str]:
        return [raw]


# Heuristic for the most common uncited-claim failure mode: the model names a
# specific study in traditional author-year prose style ("Madhusudhan et al.
# (2011a,b)") instead of — or in addition to — attaching the required [n]
# marker. This does NOT catch every uncited claim (a claim with no author
# name at all slips through), but it's a strong, low-false-positive signal:
# verified against the already-generated corpus, 24/50 author-year mentions
# sampled across 40 paper pages had no [n] within 60 chars, meaning every
# validate_and_enrich call up to now reported 0 warnings on these while the
# citation contract was silently violated (checked only "do markers used
# resolve", never "did every claim get a marker"). This regex and its 200-char
# window are empirically calibrated ON THIS CORPUS — don't generalize it
# further without new evidence; add a new built-in style instead (see
# "don't abstract yet" in the migration plan).
_AUTHOR_YEAR_RE = re.compile(
    r"[A-Z][a-zà-ÿ]+(?:\s+(?:et al\.?|&|and)\s*[A-Za-zà-ÿ]*)?"
    r"\s*\((?:19|20)\d{2}[a-z]?(?:,\s*(?:19|20)?\d{2,4}[a-z]?)*\)"
)


def _split_author_names(authors: str | list[str]) -> list[str]:
    if isinstance(authors, list):
        return [a.split(",")[0].strip() if "," in a else a.strip() for a in authors if a.strip()]
    return [a.split(",")[0].strip() for a in (authors or "").split(";") if a.strip()]


class AuthorYearStyle(_BaseCitationStyle):
    """Default style: bibcode-shaped doc_ids get a bare-year chip label;
    authors render as 'Lastname (& Lastname2 | et al.) YYYY', matching the
    ADS-style 'Last, First; Last, First; ...' authors string convention
    already used in agent-published answers."""

    def label(self, ref: DocRef) -> str:
        if ref.doc_id[:4].isdigit():
            return ref.doc_id[:4]
        return str(ref.year) if ref.year else ref.doc_id

    def reference_label(self, ref: DocRef) -> str:
        names = _split_author_names(ref.authors)
        y = str(ref.year) if ref.year else ""
        if not names:
            return (ref.title[:40] if ref.title else "") or y or ref.doc_id
        if len(names) == 1:
            return f"{names[0]} {y}".strip()
        if len(names) == 2:
            return f"{names[0]} & {names[1]} {y}".strip()
        return f"{names[0]} et al. {y}".strip()

    def mention_pattern(self):
        return _AUTHOR_YEAR_RE

    def mention_window(self) -> int:
        return 200


class PlainStyle(_BaseCitationStyle):
    """For a corpus with no author-year convention worth enforcing (e.g. a
    minimal filename-doc_id adapter, 05_adoption.md) — labels fall back to
    doc_id/title, and the uncited-mention heuristic is off (mention_pattern
    returns None) rather than false-positiving on prose it was never
    calibrated against."""

    def label(self, ref: DocRef) -> str:
        return ref.doc_id

    def reference_label(self, ref: DocRef) -> str:
        return (ref.title[:40] if ref.title else ref.doc_id)

    def mention_pattern(self):
        return None

    def mention_window(self) -> int:
        return 200


_BUILTINS = {"author_year": AuthorYearStyle, "plain": PlainStyle}


def load_citation_style(corpus) -> CitationStyle:
    """corpus.yaml's `identity:` block selects a style by name
    (`citation_style: author_year|plain`, default author_year) plus an
    optional declarative `external_links:` list. A corpus needing anything
    beyond these two builtins (e.g. wasp12b's '&'->'+' doc_id encoding —
    see corpora/wasp12b/style.py) points `citation_style` at a
    corpus-relative "./style.py:ClassName" path instead, loaded the same
    way Corpus.adapter_module() loads metadata_adapter."""
    identity_cfg = (corpus.raw or {}).get("identity") or {}
    kind = identity_cfg.get("citation_style", "author_year")
    link_specs = identity_cfg.get("external_links") or []

    if kind in _BUILTINS:
        return _BUILTINS[kind](link_specs)

    # "./style.py:ClassName" or a dotted "module.path:ClassName" — same
    # resolution Corpus.load_module() gives metadata_adapter, reused
    # directly rather than duplicated.
    mod_path, class_name = kind.split(":")
    module = corpus.load_module(mod_path)
    return getattr(module, class_name)(link_specs)
