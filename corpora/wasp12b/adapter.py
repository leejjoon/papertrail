"""
Metadata adapter for the WASP-12b corpus (corpus.yaml's metadata_adapter).

Thin wrapper around paper_download/config.load_papers() — no new metadata-loading
code, just reshaping into the generic {doc_id, title, authors, year, journal, extra}
interface every pipeline stage expects (docs/spec/01_data_contracts.md).

<- kb/adapters/wasp12b.py, moved here in the papertrail migration's Step 8 (making
this corpus a self-contained directory: corpus.yaml + adapter.py + data/ + wiki/
under one root). paper_download/ stays corpus-side but OUTSIDE this directory (it's
shared acquisition tooling, not corpus data — see CLAUDE.md's repo layout), so this
still needs to reach across to it; the difference from before is HOW: an explicit
importlib.util.spec_from_file_location instead of a sys.path.insert() + bare
`import config`, which collided with kb/config.py's own module name and depended on
import order. A unique spec name here means no collision is possible regardless of
what else is on sys.path.
"""
import importlib.util
from pathlib import Path

from papertrail.textutils import slugify

_PAPER_DOWNLOAD_DIR = Path(__file__).resolve().parents[2] / "paper_download"


def _load_pd_config():
    spec = importlib.util.spec_from_file_location(
        "wasp12b_paper_download_config", _PAPER_DOWNLOAD_DIR / "config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_pd_config = _load_pd_config()


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
    with has_fulltext so Stage E (papertrail.pipeline.index) can insert stub `docs`
    rows for the 20 that never got a PDF. Not used by Stages A-D, which only operate
    on fulltext-bearing papers via load(). This is an undeclared second entry point
    corpus.yaml doesn't mention — see papertrail migration Step 9 for turning it into
    a proper declared one."""
    import json

    manifest_path = _PAPER_DOWNLOAD_DIR / "data" / "pdf_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}

    all_raw = _pd_config.load_papers(include_skipped=True)
    out = []
    for p in all_raw:
        bibcode = p.get("bibcode")
        safe = _pd_config.safe_bibcode(bibcode) if bibcode else slugify(p["title"])
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
