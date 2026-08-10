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

Step 9: load() and the former load_all_including_stubs() are collapsed into this
one function, declared in corpus.yaml as the single metadata_adapter entry point.
There used to be two: load() returned only the 190 fetchable/full-text papers (via
paper_download's default skip_reason() filtering) with no has_fulltext/
fulltext_source fields at all, while a second, UNDECLARED entry point,
load_all_including_stubs(), returned all 210 (190 + 20 skipped-by-design/missing)
WITH those fields computed from the PDF manifest — used only by
papertrail.pipeline.index (Stage E) via a bare `from kb.adapters import wasp12b`
that bypassed corpus.yaml's metadata_adapter mechanism entirely. Verified the merge
is safe: the new has_fulltext=True subset is exactly the 190 doc_ids that have a
real Marker JSON file on disk (paper_download/data/marker/*.json), identical to
what the old load() implicitly produced by relying on Stage A's own
marker-file-existence check.
"""
import importlib.util
import json
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
    """One dict per known WASP-12b-mentioning paper (210 total: 190 with real
    full text fetched + 20 skipped-by-design/missing — see
    docs/spec/00_overview.md's "Corpus completeness"), each tagged with
    has_fulltext/fulltext_source (computed from the PDF fetch manifest) so
    every pipeline stage can tell full-text docs from metadata-only stubs
    without a second entry point. Stages A-D naturally only ever act on
    doc_ids that also have a Marker JSON file (a stronger, independent
    signal than has_fulltext, though the two agree exactly for this
    corpus — verified); Stage E uses has_fulltext directly to decide which
    docs get a full node tree vs. a single stub node."""
    manifest_path = _PAPER_DOWNLOAD_DIR / "data" / "pdf_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}

    all_raw = _pd_config.load_papers(include_skipped=True)
    out = []
    for p in all_raw:
        bibcode = p.get("bibcode")
        doc_id = p["safe_bibcode"] if p.get("safe_bibcode") else slugify(p["title"])
        entry = manifest.get(bibcode) if bibcode else None
        has_fulltext = bool(entry and entry.get("status") == "ok")
        fulltext_source = None
        if has_fulltext:
            fulltext_source = "arxiv_preprint" if entry.get("source") == "arxiv_manual" else "publisher_pdf"
        out.append({
            "doc_id": doc_id,
            "native_id": bibcode,
            "bibcode": bibcode,
            "title": p["title"],
            "authors": p.get("authors"),
            "year": p.get("year"),
            "journal": p["journal"]["name"],
            "has_fulltext": has_fulltext,
            "fulltext_source": fulltext_source,
            "skip_reason": p.get("skip_reason"),
            "extra": {
                "doi": p.get("doi"),
                "ads_url": p.get("ads_url"),
            },
        })
    return out
