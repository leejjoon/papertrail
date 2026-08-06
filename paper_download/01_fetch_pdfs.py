#!/usr/bin/env python3
"""Step 1 — Download published-version PDFs for all ai_ml_papers.

Publisher-direct only (no arXiv, no Unpaywall): meant to run on a machine
inside the institute network, where subscription access to the publishers
applies. For each paper:

  1. One batched ADS query up front fetches `esources` per bibcode.
  2. If PUB_PDF is available, download via the ADS link gateway
     (https://ui.adsabs.harvard.edu/link_gateway/<bibcode>/PUB_PDF),
     which 302-redirects to the publisher's PDF.
  3. Validate: starts with %PDF-, size >= 20 KB. HTML interstitials count
     as failures.

Manually downloaded PDFs are first-class: drop a file at
kb/data/pdfs/<safe_bibcode>.pdf and re-run — it is validated and recorded
with source=manual, nothing is re-fetched.

Per-publisher access knowledge (which method works where, Cloudflare handling,
unsolved cases) is documented in kb/PUBLISHER_ACCESS.md.

State lives in kb/data/pdf_manifest.json (one record per bibcode). The run
ends with a success/failure report broken down by journal and failure reason.

Usage:
    python3 01_fetch_pdfs.py                 # fetch everything missing
    python3 01_fetch_pdfs.py --dry-run       # show what would be fetched
    python3 01_fetch_pdfs.py --retry-failed  # retry only failed entries
    python3 01_fetch_pdfs.py --only 2021SpWea..1902741K
    python3 01_fetch_pdfs.py --limit 5       # first 5 pending papers only
"""
import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent))
from config import ADS_API_TOKEN, PDF_DIR, PDF_MANIFEST, load_papers

ADS_SEARCH_URL = "https://api.adsabs.harvard.edu/v1/search/query"
LINK_GATEWAY = "https://ui.adsabs.harvard.edu/link_gateway/{bibcode}/PUB_PDF"
BATCH_SIZE = 100
MIN_PDF_BYTES = 20_000
SLEEP_BETWEEN = 1.5

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
                   "Gecko/20100101 Firefox/128.0"),
    "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def load_manifest() -> dict:
    if PDF_MANIFEST.exists():
        return json.loads(PDF_MANIFEST.read_text(encoding="utf-8"))
    return {}


def save_manifest(manifest: dict) -> None:
    tmp = PDF_MANIFEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, PDF_MANIFEST)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_pdf(path: Path) -> str | None:
    """Return a failure reason, or None if the file looks like a real PDF."""
    if not path.exists():
        return "file missing"
    size = path.stat().st_size
    with open(path, "rb") as f:
        head = f.read(5)
    if head != b"%PDF-":
        return "not a PDF (got HTML/other)"
    if size < MIN_PDF_BYTES:
        return f"too small ({size} bytes)"
    return None


def fetch_esources(bibcodes: list[str], retries: int = 3) -> dict:
    """One batched ADS query: {bibcode: [esource, ...]}."""
    headers = {"Authorization": f"Bearer {ADS_API_TOKEN}"}
    results: dict[str, list] = {}
    batches = [bibcodes[i:i + BATCH_SIZE]
               for i in range(0, len(bibcodes), BATCH_SIZE)]
    for batch in batches:
        params = {
            "q": "bibcode:(" + " OR ".join(batch) + ")",
            "fl": "bibcode,esources",
            "rows": len(batch),
        }
        for attempt in range(retries):
            resp = requests.get(ADS_SEARCH_URL, headers=headers,
                                params=params, timeout=30)
            if resp.status_code == 429:
                wait = 60 * (attempt + 1)
                tqdm.write(f"ADS rate limited — waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            for doc in resp.json().get("response", {}).get("docs", []):
                results[doc["bibcode"]] = doc.get("esources", [])
            break
        else:
            raise RuntimeError(f"ADS request failed after {retries} retries")
        if len(batches) > 1:
            time.sleep(0.3)
    return results


def download_pdf(session: requests.Session, bibcode: str,
                 target: Path) -> tuple[bool, str | None, int | None, str | None]:
    """Download via the ADS link gateway. Returns (ok, error, http_status, final_url)."""
    url = LINK_GATEWAY.format(bibcode=quote(bibcode, safe=""))
    part = target.with_suffix(".pdf.part")
    try:
        with session.get(url, headers=BROWSER_HEADERS, timeout=60,
                         stream=True, allow_redirects=True) as resp:
            status = resp.status_code
            final_url = resp.url
            if status != 200:
                return False, f"HTTP {status}", status, final_url
            with open(part, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
    except requests.RequestException as e:
        part.unlink(missing_ok=True)
        return False, f"{type(e).__name__}: {e}", None, None

    reason = validate_pdf(part)
    if reason:
        part.unlink(missing_ok=True)
        return False, reason, status, final_url
    os.replace(part, target)
    return True, None, status, final_url


# PDF-link selectors on a landing page, in priority order (same set as 01b).
PDF_SELECTORS = ("a[href*='article-pdf']", "a[href*='/doi/pdf/']",
                 "a[href*='/content/pdf/']", "a[href*='download_pdf']",
                 "a[href$='.pdf']")


def find_pdf_url_in_html(html: str, base_url: str) -> str | None:
    """Extract a PDF URL from landing-page HTML: citation_pdf_url meta first
    (the Google-Scholar metadata standard), then common link patterns."""
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", attrs={"name": "citation_pdf_url"})
    if meta and meta.get("content"):
        return urljoin(base_url, meta["content"])
    for selector in PDF_SELECTORS:
        link = soup.select_one(selector)
        if link and link.get("href"):
            return urljoin(base_url, link["href"])
    return None


def download_via_landing_page(session: requests.Session, doi: str,
                              target: Path) -> tuple[bool, str | None, int | None, str | None]:
    """Resolve the DOI to a landing page, find its PDF link, and download it.
    Returns (ok, error, http_status, pdf_url)."""
    try:
        landing = session.get(f"https://doi.org/{doi}", headers=BROWSER_HEADERS,
                              timeout=60, allow_redirects=True)
    except requests.RequestException as e:
        return False, f"landing page: {type(e).__name__}: {e}", None, None
    if landing.status_code != 200:
        return False, f"landing HTTP {landing.status_code}", landing.status_code, landing.url

    pdf_url = find_pdf_url_in_html(landing.text, landing.url)
    if not pdf_url:
        return False, "no PDF link on landing page", landing.status_code, landing.url

    part = target.with_suffix(".pdf.part")
    try:
        with session.get(pdf_url, headers=BROWSER_HEADERS, timeout=60,
                         stream=True, allow_redirects=True) as resp:
            if resp.status_code != 200:
                return False, f"PDF HTTP {resp.status_code}", resp.status_code, pdf_url
            with open(part, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
    except requests.RequestException as e:
        part.unlink(missing_ok=True)
        return False, f"PDF fetch: {type(e).__name__}: {e}", None, pdf_url

    reason = validate_pdf(part)
    if reason:
        part.unlink(missing_ok=True)
        return False, reason, 200, pdf_url
    os.replace(part, target)
    return True, None, 200, pdf_url


def record(manifest: dict, paper: dict, **fields) -> None:
    entry = {
        "safe_bibcode": paper["safe_bibcode"],
        "doi": paper["doi"],
        "journal": paper["journal"]["name"],
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    entry.update(fields)
    manifest[paper["bibcode"]] = entry
    save_manifest(manifest)


def print_report(manifest: dict, papers: list[dict]) -> None:
    by_bibcode = {p["bibcode"]: p for p in papers}
    ok = {bc: e for bc, e in manifest.items() if e["status"] == "ok"}
    failed = {bc: e for bc, e in manifest.items() if e["status"] == "failed"}
    pending = [bc for bc in by_bibcode if bc not in manifest]

    print("\n" + "=" * 70)
    print(f"PDF ACQUISITION REPORT — {len(papers)} papers")
    print("=" * 70)
    print(f"  OK      : {len(ok)}"
          f"  (downloaded: {sum(1 for e in ok.values() if e['source'] != 'manual')},"
          f" manual: {sum(1 for e in ok.values() if e['source'] == 'manual')})")
    print(f"  FAILED  : {len(failed)}")
    if pending:
        print(f"  PENDING : {len(pending)} (not attempted this run)")

    def journal_counter(entries):
        return Counter(e["journal"].split("-")[0].strip() for e in entries.values())

    print("\nBy journal (ok / failed):")
    all_journals = journal_counter(ok) + journal_counter(failed)
    ok_j, failed_j = journal_counter(ok), journal_counter(failed)
    for journal in sorted(all_journals):
        print(f"  {journal[:52]:<52} {ok_j.get(journal, 0):>3} / {failed_j.get(journal, 0):>3}")

    if failed:
        print("\nFailure reasons:")
        for reason, n in Counter(e["error"] for e in failed.values()).most_common():
            print(f"  {n:>3}  {reason}")
        print("\nFailed papers (for manual download → kb/data/pdfs/<safe_bibcode>.pdf):")
        print(f"  {'bibcode':<22} {'target filename':<26} doi")
        for bc, e in sorted(failed.items()):
            print(f"  {bc:<22} {e['safe_bibcode'] + '.pdf':<26} https://doi.org/{e['doi']}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Download published PDFs via ADS link gateway.")
    parser.add_argument("--dry-run", action="store_true", help="Report without downloading")
    parser.add_argument("--force", action="store_true", help="Re-download even if PDF exists")
    parser.add_argument("--retry-failed", action="store_true", help="Retry only status=failed entries")
    parser.add_argument("--only", metavar="BIBCODE", help="Process a single bibcode")
    parser.add_argument("--limit", type=int, help="Process at most N pending papers")
    parser.add_argument("--sleep", type=float, default=SLEEP_BETWEEN,
                        help=f"Seconds between downloads (default {SLEEP_BETWEEN})")
    args = parser.parse_args()

    if not ADS_API_TOKEN:
        sys.exit("ADS_API_TOKEN not found in .env")

    papers = load_papers()
    manifest = load_manifest()
    print(f"Loaded {len(papers)} papers | manifest entries: {len(manifest)}")

    # Pass 0: pick up PDFs already on disk (downloaded earlier or dropped manually)
    for paper in papers:
        bc = paper["bibcode"]
        pdf_path = PDF_DIR / f"{paper['safe_bibcode']}.pdf"
        if not pdf_path.exists():
            continue
        entry = manifest.get(bc)
        if entry and entry["status"] == "ok" and not args.force:
            continue
        reason = validate_pdf(pdf_path)
        if reason:
            tqdm.write(f"  Invalid existing PDF for {bc}: {reason} — will re-fetch")
            pdf_path.unlink()
            continue
        source = entry["source"] if entry and entry.get("status") == "ok" else "manual"
        record(manifest, paper,
               status="ok", source=source, url=entry.get("url") if entry else None,
               http_status=entry.get("http_status") if entry else None,
               sha256=sha256_of(pdf_path), size_bytes=pdf_path.stat().st_size,
               error=None)

    # Select papers to attempt
    todo = []
    for paper in papers:
        bc = paper["bibcode"]
        if args.only and bc != args.only:
            continue
        entry = manifest.get(bc)
        if entry and entry["status"] == "ok" and not args.force:
            continue
        if args.retry_failed and not (entry and entry["status"] == "failed"):
            continue
        todo.append(paper)
    if args.limit:
        todo = todo[:args.limit]

    print(f"To attempt: {len(todo)}")
    if not todo:
        print_report(manifest, papers)
        return

    print("Querying ADS for esources (one batched query)...")
    esources = fetch_esources([p["bibcode"] for p in todo])
    no_pub_pdf = [p["bibcode"] for p in todo
                  if "PUB_PDF" not in esources.get(p["bibcode"], [])]
    print(f"  PUB_PDF available: {len(todo) - len(no_pub_pdf)} / {len(todo)}")

    if args.dry_run:
        for p in todo:
            has = "PUB_PDF" if "PUB_PDF" in esources.get(p["bibcode"], []) else "NO PUB_PDF"
            print(f"  [dry-run] {p['bibcode']:<22} {has:<12} {p['journal']['name'][:40]}")
        return

    session = requests.Session()
    for paper in tqdm(todo, unit="pdf", desc="Downloading"):
        bc = paper["bibcode"]
        pdf_path = PDF_DIR / f"{paper['safe_bibcode']}.pdf"

        # 1. ADS link gateway (PUB_PDF), when ADS has that esource.
        ok = False
        source = None
        errors = []
        if "PUB_PDF" in esources.get(bc, []):
            ok, error, status, final_url = download_pdf(session, bc, pdf_path)
            source = "ads_pub_pdf"
            if not ok:
                errors.append(f"ads_pub_pdf: {error}")

        # 2. DOI landing-page scrape (citation_pdf_url meta / PDF links) —
        #    covers publishers with no PUB_PDF esource and gateway failures.
        if not ok:
            ok, error, status, final_url = download_via_landing_page(
                session, paper["doi"], pdf_path)
            source = "landing_page"
            if not ok:
                errors.append(f"landing_page: {error}")

        if ok:
            record(manifest, paper, status="ok", source=source,
                   url=final_url, http_status=status,
                   sha256=sha256_of(pdf_path),
                   size_bytes=pdf_path.stat().st_size, error=None)
        else:
            record(manifest, paper, status="failed", source=source,
                   url=final_url, http_status=status, sha256=None,
                   size_bytes=None, error=" | ".join(errors))
            tqdm.write(f"  FAILED {bc}: {' | '.join(errors)}")
        time.sleep(args.sleep)

    print_report(manifest, papers)


if __name__ == "__main__":
    main()
