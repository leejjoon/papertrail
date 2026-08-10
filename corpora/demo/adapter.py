"""Minimal metadata adapter — the "no existing metadata file, just a
directory of Marker JSONs" tier `05_adoption.md` (step 2 of its checklist)
describes. Written for `papertrail`'s Step 12: a throwaway demo corpus
proving the pipeline genuinely needs nothing WASP-12b-shaped.

Deliberately dumb: this corpus has no `wasp_12b_papers.yaml`/ADS-metadata
equivalent at all, so every field beyond `doc_id` is derived straight from
the Marker JSON itself (title = the first SectionHeader's text) or left
`None` (year/authors/journal — a real corpus with this info would supply it
instead). `native_id` is omitted entirely, `has_fulltext`/`fulltext_source`
are always True/"publisher_pdf" since this demo only lists papers known to
have a real marker JSON on disk.

Reuses a handful of real WASP-12b Marker JSONs (`paper_download/data/marker/`)
purely as convenient, already-on-disk fixture data — the corpus.yaml this
adapter serves configures a deliberately different `subject`/taxonomy/wiki
headings than corpora/wasp12b/, which is the actual thing being tested:
that nothing downstream hardcodes WASP-12b, only corpus.yaml does.
"""
import json
import re
from pathlib import Path

# Fixed, small subset — this is a throwaway proof corpus, not a growing one.
_DOC_IDS = [
    "2012ApJ...746...46C",
    "2012ApJ...745...77K",
    "2010ApJ...716L..36L",
    "2012ApJ...757....6H",
    "2012ApJ...750..133H",
]

_MARKER_DIR = Path(__file__).resolve().parent.parent.parent / "paper_download" / "data" / "marker"


def _first_heading_text(doc: dict) -> str | None:
    """Depth-first search for the first SectionHeader block's text (an
    <h1>/<h2>/... tag), stripped of markup — this IS the "derive a title
    from the Marker JSON itself" rule 05_adoption.md's checklist describes
    for a corpus with no metadata file of its own."""
    for page in doc.get("children") or []:
        for block in page.get("children") or []:
            if block.get("block_type") == "SectionHeader":
                text = re.sub(r"<[^>]+>", " ", block.get("html") or "")
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    return text
    return None


def load() -> list[dict]:
    out = []
    for doc_id in _DOC_IDS:
        marker_path = _MARKER_DIR / f"{doc_id}.json"
        title = None
        if marker_path.exists():
            raw = json.loads(marker_path.read_text(encoding="utf-8"))
            doc = raw.get("json")
            if isinstance(doc, str):
                doc = json.loads(doc)
            title = _first_heading_text(doc or {})
        out.append({
            "doc_id": doc_id,
            "title": title or doc_id,
            "authors": None,
            "year": None,
            "journal": None,
            "extra": {},
            "has_fulltext": True,
            "fulltext_source": "publisher_pdf",
        })
    return out
