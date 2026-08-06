"""
paper_download configuration — loads ADS credentials from ../.env, defines
paths, and adapts wasp_12b_papers.yaml (a single flat YAML) into the paper
records that 01_fetch_pdfs.py / 01b_fetch_pdfs_browser.py expect.

Never log or print API keys.
"""
import json
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv
import os

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

ADS_API_TOKEN: str = os.getenv("ADS_API_TOKEN", "")
DATALAB_API_KEY: str = os.getenv("DATALAB_API_KEY", "")

PAPERS_YAML = ROOT / "wasp_12b_papers.yaml"

KB_DIR = Path(__file__).parent
DATA_DIR = KB_DIR / "data"
PDF_DIR = DATA_DIR / "pdfs"
PDF_MANIFEST = DATA_DIR / "pdf_manifest.json"
ADS_META = DATA_DIR / "ads_meta.json"
MARKER_DIR = DATA_DIR / "marker"
MARKER_MANIFEST = DATA_DIR / "marker_manifest.json"

for _d in (DATA_DIR, PDF_DIR, MARKER_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def safe_bibcode(bibcode: str) -> str:
    """Filesystem-safe bibcode: '&' (A&A) -> '+'. '+' never occurs in bibcodes."""
    return bibcode.replace("&", "+")


def unsafe_bibcode(safe: str) -> str:
    return safe.replace("+", "&")


# Journal-name patterns that mean "not a fetchable journal article" — meeting
# abstracts, book chapters, arXiv-only entries. Kept as one auditable function
# rather than silently dropping rows in load_papers().
_SKIP_PATTERNS = [
    re.compile(r"Meeting Abstract", re.IGNORECASE),
    re.compile(r"#\d+"),                       # "...Meeting Abstracts #213"
    re.compile(r"^arXiv e-prints$", re.IGNORECASE),
    re.compile(r"Handbook of Exoplanets", re.IGNORECASE),
    re.compile(r"^Exoplanets$"),                # book "Exoplanets" (ed. Seager)
    re.compile(r"Exoplanet Handbook", re.IGNORECASE),
    re.compile(r"Instrument Science Report", re.IGNORECASE),
]


def skip_reason(paper: dict) -> str | None:
    """Return a human-readable reason this entry can't be fetched by the
    publisher-direct cascade, or None if it should be attempted."""
    if not paper.get("bibcode"):
        return "no bibcode"
    journal = paper.get("journal") or ""
    for pat in _SKIP_PATTERNS:
        if pat.search(journal):
            return f"not a journal article ({journal!r})"
    return None


def _load_ads_meta() -> dict:
    if ADS_META.exists():
        return json.loads(ADS_META.read_text(encoding="utf-8"))
    return {}


def load_papers(include_skipped: bool = False) -> list[dict]:
    """Read wasp_12b_papers.yaml, adapt each record for the fetch scripts.

    Adds: safe_bibcode, doi (from data/ads_meta.json, if fetched), esources,
    journal={'name': ...}. Drops entries flagged by skip_reason() unless
    include_skipped=True (in which case they get skip_reason set instead).
    """
    data = yaml.safe_load(PAPERS_YAML.read_text(encoding="utf-8"))
    ads_meta = _load_ads_meta()

    papers = []
    for raw in data["papers"]:
        reason = skip_reason(raw)
        if reason and not include_skipped:
            continue
        bibcode = raw.get("bibcode")
        meta = ads_meta.get(bibcode, {}) if bibcode else {}
        paper = dict(raw)
        paper["safe_bibcode"] = safe_bibcode(bibcode) if bibcode else None
        paper["doi"] = meta.get("doi")
        paper["esources"] = meta.get("esources", [])
        paper["journal"] = {"name": raw.get("journal") or "unknown"}
        if reason:
            paper["skip_reason"] = reason
        papers.append(paper)
    return papers


def load_all_papers() -> list[dict]:
    """All 210 raw YAML entries (for reporting), unadapted."""
    data = yaml.safe_load(PAPERS_YAML.read_text(encoding="utf-8"))
    return data["papers"]
