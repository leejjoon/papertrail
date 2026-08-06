"""
Metadata adapter for the WASP-12b corpus (kb/corpus.yaml's metadata_adapter).

Thin wrapper around paper_download/config.load_papers() — no new metadata-loading
code, just reshaping into the generic {doc_id, title, authors, year, journal, extra}
interface every kb/ pipeline stage expects (docs/spec/01_data_contracts.md).
"""
import sys
from pathlib import Path

_PAPER_DOWNLOAD_DIR = Path(__file__).parent.parent.parent / "paper_download"
if str(_PAPER_DOWNLOAD_DIR) not in sys.path:
    sys.path.insert(0, str(_PAPER_DOWNLOAD_DIR))

import config as _pd_config  # paper_download/config.py

# kb/config.py's slugify (paper_download/config.py has no equivalent) — imported
# lazily inside the function that needs it, to avoid a circular import at module
# load time (kb/config.py itself dynamically imports this adapter module).


def load() -> list[dict]:
    """One dict per fetchable WASP-12b paper (the 190 with a bibcode + real journal
    article, per paper_download/config.py's skip_reason() filtering — see
    docs/spec/01_data_contracts.md for how the 20 skipped/missing entries are
    handled separately, as kb.sqlite stub rows rather than through this adapter)."""
    papers = _pd_config.load_papers()
    out = []
    for p in papers:
        out.append({
            "doc_id": p["safe_bibcode"],
            "bibcode": p["bibcode"],
            "title": p["title"],
            "authors": p.get("authors"),
            "year": p.get("year"),
            "journal": p["journal"]["name"],
            "extra": {
                "doi": p.get("doi"),
                "ads_url": p.get("ads_url"),
            },
        })
    return out


def load_all_including_stubs() -> list[dict]:
    """All 210 entries (190 fetchable + 20 skipped-by-design/missing), each tagged
    with has_fulltext so kb/build_index.py (Stage E) can insert stub `docs` rows for
    the 20 that never got a PDF. Not used by Stages A-D, which only operate on
    fulltext-bearing papers via load()."""
    import json

    from kb import config as _kb_config  # for slugify() only — see note above

    manifest_path = _PAPER_DOWNLOAD_DIR / "data" / "pdf_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}

    all_raw = _pd_config.load_papers(include_skipped=True)
    out = []
    for p in all_raw:
        bibcode = p.get("bibcode")
        safe = _pd_config.safe_bibcode(bibcode) if bibcode else _kb_config.slugify(p["title"])
        entry = manifest.get(bibcode) if bibcode else None
        has_fulltext = bool(entry and entry.get("status") == "ok")
        fulltext_source = None
        if has_fulltext:
            fulltext_source = "arxiv_preprint" if entry.get("source") == "arxiv_manual" else "publisher_pdf"
        out.append({
            "doc_id": safe,
            "bibcode": bibcode,
            "title": p["title"],
            "authors": p.get("authors"),
            "year": p.get("year"),
            "journal": (p.get("journal") or {}).get("name") if isinstance(p.get("journal"), dict) else p.get("journal"),
            "has_fulltext": has_fulltext,
            "fulltext_source": fulltext_source,
            "skip_reason": p.get("skip_reason"),
            "extra": {"doi": p.get("doi"), "ads_url": p.get("ads_url")},
        })
    return out
