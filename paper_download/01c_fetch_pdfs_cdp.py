#!/usr/bin/env python3
"""Step 1c — Retry outstanding PDFs via a real, human-launched Chrome over CDP.

Unlike 01b (which launches its own isolated Playwright-controlled browser),
this connects to an *already-running* Chrome started with
--remote-debugging-port=9222 — per PAPER_DOWNLOAD_HOWTO.md §3, that's what it
takes to clear Elsevier/ScienceDirect's anti-bot challenge (only a trusted,
non-automated navigation + a real click gets through), and it doubles as the
vehicle for IP-gated subscription content once that Chrome is routed through
the KASI SOCKS tunnel (--proxy-server=socks5://127.0.0.1:1080).

Requires, on the already-running Chrome:
  - chrome://settings/content/pdfDocuments -> "Download PDFs" (not "Open PDFs
    in Chrome"), i.e. profile pref plugins.always_open_pdf_externally = true.
  - Routed through the KASI tunnel if fetching subscription content off-site.

Downloads are captured by polling a fixed directory (not page.expect_download()
— unreliable for a browser Playwright didn't launch) after
Browser.setDownloadBehavior(allowAndName) is set via a CDP session.

Usage:
    python3 01c_fetch_pdfs_cdp.py --only 2013Icar..225..432S   # single paper
    python3 01c_fetch_pdfs_cdp.py --all-failed                 # every failed entry
    python3 01c_fetch_pdfs_cdp.py --all-failed --limit 5
    CDP_ENDPOINT=http://127.0.0.1:9223 python3 01c_fetch_pdfs_cdp.py --only ...
"""
import argparse
import importlib.util
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

KB_DIR = Path(__file__).parent
sys.path.insert(0, str(KB_DIR))
from config import PDF_DIR, load_papers

# 01_fetch_pdfs.py starts with a digit — load it via importlib for its helpers
# (load_manifest, record, sha256_of, MIN_PDF_BYTES), same trick 01b uses.
_spec = importlib.util.spec_from_file_location("fetch_pdfs", KB_DIR / "01_fetch_pdfs.py")
fetch_pdfs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetch_pdfs)

CDP_ENDPOINT = os.environ.get("CDP_ENDPOINT", "http://127.0.0.1:9222")
DOWNLOAD_DIR = KB_DIR / "data" / "cdp_downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# href substrings ranked by how likely they are to be the *article* PDF
# (not a boilerplate leaflet/guide asset that happens to also end in .pdf).
HREF_RANK = ("pdfft", "article-pdf", "/doi/pdfdirect/", "/doi/pdf/",
             "/content/pdf/", "ampdf", "download_pdf", ".pdf")
# hrefs containing any of these are never the article (site boilerplate).
HREF_EXCLUDE = ("/images/", "leaflet", "aa_leaflet", "-guide", "_guide",
                "cookie_policy", "journal_price", "citation-needed",
                "media.springernature.com", "silverchair-cdn.com/",
                "DownloadImage.aspx")
TEXT_HINT = re.compile(r"^\s*(download\s+)?pdf\b", re.IGNORECASE)

CONSENT_BUTTON_TEXTS = ("accept all cookies", "accept all", "i accept", "agree")


def dismiss_consent(page) -> None:
    """Best-effort: click a cookie-consent 'accept' button if one is up front.
    Springer/OUP/IOP overlays otherwise intercept pointer events and time out
    a real click on the actual PDF link underneath."""
    for text in CONSENT_BUTTON_TEXTS:
        btn = page.get_by_role("button", name=text, exact=False)
        try:
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=3000)
                page.wait_for_timeout(500)
                return
        except Exception:
            continue


def find_pdf_candidates(page, timeout_s: int = 20) -> list[dict]:
    """All <a> elements that look like the article PDF, ranked best-first.
    Considers href pattern, link text ('PDF (354 KB)', 'Download PDF'), and
    visibility — duplicated nav elements (mobile/desktop) mean the *first*
    DOM match for a given selector is not reliably the real, visible one."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        links = page.eval_on_selector_all(
            "a", "els => els.map(e => ({href: e.href, "
                 "text: e.innerText.trim(), visible: e.offsetParent !== null}))")
        candidates = []
        for l in links:
            href = l.get("href") or ""
            if not href or any(x in href for x in HREF_EXCLUDE):
                continue
            href_rank = next((i for i, pat in enumerate(HREF_RANK) if pat in href), None)
            text_hit = bool(TEXT_HINT.match(l.get("text") or ""))
            if href_rank is None and not text_hit:
                continue
            score = (0 if l["visible"] else 100) + (href_rank if href_rank is not None else 8) \
                    - (2 if text_hit else 0)
            candidates.append((score, l))
        if candidates:
            candidates.sort(key=lambda t: t[0])
            return [l for _, l in candidates]
        page.wait_for_timeout(1000)
    return []


def poll_new_file(before: set[str], timeout_s: int = 45) -> Path | None:
    """Wait for a new, fully-written file (not .crdownload/.tmp) to appear."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        after = set(os.listdir(DOWNLOAD_DIR))
        done = [f for f in (after - before) if not f.endswith((".crdownload", ".tmp"))]
        if done:
            # give the write a moment to flush before reading
            time.sleep(0.5)
            return DOWNLOAD_DIR / done[0]
        time.sleep(1)
    return None


def process_one(page, paper: dict, manifest: dict) -> bool:
    bc = paper["bibcode"]
    target = PDF_DIR / f"{paper['safe_bibcode']}.pdf"
    doi = paper["doi"]
    if not doi:
        print(f"  SKIP {bc}: no DOI on record")
        return False

    try:
        page.goto(f"https://doi.org/{doi}", timeout=60_000, wait_until="domcontentloaded")
    except Exception as e:
        print(f"  FAILED {bc}: landing nav: {type(e).__name__}: {str(e)[:80]}")
        fetch_pdfs.record(manifest, paper, status="failed", source="cdp",
                          url=None, http_status=None, sha256=None,
                          size_bytes=None, error=f"landing nav: {e}")
        return False
    page.wait_for_timeout(2000)
    dismiss_consent(page)

    candidates = find_pdf_candidates(page)
    if not candidates:
        fetch_pdfs.record(manifest, paper, status="failed", source="cdp",
                          url=page.url, http_status=None, sha256=None,
                          size_bytes=None, error="no PDF link found (CDP pass)")
        print(f"  FAILED {bc}: no PDF link found on {page.url.split('/')[2]}")
        return False

    def save(body: bytes, url: str) -> bool:
        target.write_bytes(body)
        fetch_pdfs.record(manifest, paper, status="ok", source="cdp",
                          url=url, http_status=200,
                          sha256=fetch_pdfs.sha256_of(target),
                          size_bytes=target.stat().st_size, error=None)
        print(f"  OK {bc} ({target.stat().st_size:,} bytes)")
        return True

    errors = []
    top = candidates[0]

    # 1. Direct authenticated GET (fast, no click needed) — works once the
    #    KASI proxy grants access; Elsevier still 403s here and falls through.
    try:
        resp = page.request.get(top["href"], timeout=20_000)
        if resp.ok:
            body = resp.body()
            if body.startswith(b"%PDF-") and len(body) >= fetch_pdfs.MIN_PDF_BYTES:
                return save(body, top["href"])
            errors.append(f"direct GET: not a PDF ({resp.headers.get('content-type')})")
        else:
            errors.append(f"direct GET: HTTP {resp.status}")
    except Exception as e:
        errors.append(f"direct GET: {type(e).__name__}: {str(e)[:50]}")

    # 2. Real trusted click on the best visible candidate (Elsevier requires
    #    exactly this; also covers publishers that reject the API context).
    before = set(os.listdir(DOWNLOAD_DIR))
    clickable = next((c for c in candidates if c["visible"]), None)
    if clickable is None:
        errors.append("no visible candidate to click")
        fetch_pdfs.record(manifest, paper, status="failed", source="cdp",
                          url=page.url, http_status=None, sha256=None,
                          size_bytes=None, error="; ".join(errors))
        print(f"  FAILED {bc}: {'; '.join(errors)}")
        return False

    try:
        # Substring match on the path, not an exact match on the resolved
        # absolute href — the raw HTML attribute is often relative
        # ('/content/pdf/...'), which an exact a[href='<absolute-url>']
        # selector never matches (silently 0 elements -> click() just times
        # out waiting on an empty locator).
        path = urlsplit(clickable["href"]).path
        loc = page.locator(f"a[href*='{path}']").first
        loc.click(timeout=15_000)
    except Exception as e:
        errors.append(f"click: {type(e).__name__}: {str(e)[:50]}")
        fetch_pdfs.record(manifest, paper, status="failed", source="cdp",
                          url=page.url, http_status=None, sha256=None,
                          size_bytes=None, error="; ".join(errors))
        print(f"  FAILED {bc}: {'; '.join(errors)}")
        return False

    downloaded = poll_new_file(before)
    if not downloaded:
        errors.append("click produced no download")
        fetch_pdfs.record(manifest, paper, status="failed", source="cdp",
                          url=page.url, http_status=None, sha256=None,
                          size_bytes=None, error="; ".join(errors))
        print(f"  FAILED {bc}: {'; '.join(errors)}")
        return False

    body = downloaded.read_bytes()
    reason = None
    if not body.startswith(b"%PDF-"):
        reason = "not a PDF (got HTML/other)"
    elif len(body) < fetch_pdfs.MIN_PDF_BYTES:
        reason = f"too small ({len(body)} bytes)"
    if reason:
        downloaded.unlink(missing_ok=True)
        errors.append(reason)
        fetch_pdfs.record(manifest, paper, status="failed", source="cdp",
                          url=page.url, http_status=None, sha256=None,
                          size_bytes=None, error="; ".join(errors))
        print(f"  FAILED {bc}: {'; '.join(errors)}")
        return False

    result = save(body, clickable["href"])
    downloaded.unlink(missing_ok=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Retry failed PDFs via CDP + real Chrome.")
    parser.add_argument("--only", metavar="BIBCODE")
    parser.add_argument("--all-failed", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--sleep", type=float, default=3.0)
    args = parser.parse_args()

    manifest = fetch_pdfs.load_manifest()
    papers = {p["bibcode"]: p for p in load_papers()}

    todo = []
    for bc, entry in manifest.items():
        if entry["status"] != "failed":
            continue
        if args.only and bc != args.only:
            continue
        if not args.only and not args.all_failed:
            continue
        todo.append((papers[bc], entry))
    todo.sort(key=lambda t: t[0]["bibcode"])
    if args.limit:
        todo = todo[:args.limit]

    print(f"CDP retry targets: {len(todo)} (endpoint {CDP_ENDPOINT})")
    if not todo:
        return

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_ENDPOINT)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        cdp = context.new_cdp_session(page)
        cdp.send("Browser.setDownloadBehavior", {
            "behavior": "allowAndName",
            "downloadPath": str(DOWNLOAD_DIR),
            "eventsEnabled": True,
        })

        recovered = 0
        for i, (paper, entry) in enumerate(todo):
            print(f"[{i + 1}/{len(todo)}] {paper['bibcode']}")
            recovered += process_one(page, paper, manifest)
            if i < len(todo) - 1:
                time.sleep(args.sleep)
        page.close()
        # do NOT close/disconnect the shared browser — it's the user's own Chrome

    ok = sum(1 for e in manifest.values() if e["status"] == "ok")
    failed = sum(1 for e in manifest.values() if e["status"] == "failed")
    print(f"\nRecovered this run: {recovered}/{len(todo)}")
    print(f"Overall: {ok} ok / {failed} failed")


if __name__ == "__main__":
    main()
