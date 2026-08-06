#!/usr/bin/env python3
"""Step 1b — Recover bot-blocked PDFs with a real browser (Playwright + Chrome).

Some publishers (OUP/MNRAS, science.org) return HTTP 403 to plain requests even
from a subscribed institutional IP. This script retries exactly those manifest
entries (`status=failed, error="HTTP 403"` by default) using system Google
Chrome driven by Playwright: load the article landing page via its DOI (letting
any Cloudflare challenge resolve), then fetch the PDF through the browser's
request context, which shares cookies and TLS fingerprint.

Recovered PDFs land in kb/data/pdfs/ and the manifest entry becomes
`status=ok, source="browser"` — downstream stages need no changes.

Per-publisher access knowledge is documented in kb/PUBLISHER_ACCESS.md — read
that before adding a new publisher or debugging a failure.

Cloudflare notes (verified against academic.oup.com): headless Chrome is
detected and never clears the challenge; headful Chrome with
`--disable-blink-features=AutomationControlled` plus a `navigator.webdriver`
patch clears it in a few seconds. On a server without X, run under xvfb:

    xvfb-run -a .venv/bin/python kb/01b_fetch_pdfs_browser.py
    xvfb-run -a .venv/bin/python kb/01b_fetch_pdfs_browser.py --only 2021MNRAS.506.4121A
"""
import argparse
import importlib.util
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

KB_DIR = Path(__file__).parent
sys.path.insert(0, str(KB_DIR))
from config import PDF_DIR, load_papers

# 01_fetch_pdfs.py starts with a digit — load it via importlib for its helpers
_spec = importlib.util.spec_from_file_location("fetch_pdfs", KB_DIR / "01_fetch_pdfs.py")
fetch_pdfs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetch_pdfs)

def _find_chrome() -> str:
    """CHROME_PATH env var wins; otherwise first existing known browser binary."""
    import shutil
    candidates = [
        os.environ.get("CHROME_PATH"),
        "/opt/google/chrome/chrome",
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        "/snap/bin/chromium",
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    sys.exit("No Chrome/Chromium binary found — set CHROME_PATH")


CHROME_PATH = _find_chrome()
CHALLENGE_TITLES = re.compile(r"just a moment|attention required|access denied",
                              re.IGNORECASE)


def wait_out_challenge(page, timeout_s: int = 60) -> None:
    """Wait until the page is past the Cloudflare challenge/redirect chain."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        title = (page.title() or "").lower()
        if title and not CHALLENGE_TITLES.search(title) and not title.startswith("loading"):
            return
        page.wait_for_timeout(1000)


# On-page PDF-link selectors. Covers OUP (article-pdf), APS
# (journals.aps.org/<jrnl>/pdf/), AAS/Wiley/Science (/doi/pdf/), Springer
# (/content/pdf/), Korean OA journals (download_pdf), and generic /pdf/ / .pdf.
PDF_SELECTORS = ("a[href*='article-pdf']", "a[href*='/doi/pdf/']",
                 "a[href*='/content/pdf/']", "a[href*='download_pdf']",
                 "a[href*='/pdf/']", "a[href$='.pdf']")


def find_pdf_urls(page, recorded_url: str | None, poll_s: int = 15) -> list[str]:
    """Ordered, de-duplicated list of candidate PDF URLs to try. The
    citation_pdf_url meta tag is often a viewer page (Wiley/APS), so we keep
    on-page links as additional candidates rather than trusting one URL."""
    deadline = time.monotonic() + poll_s
    while True:
        candidates: list[str] = []
        if recorded_url and ("article-pdf" in recorded_url
                             or recorded_url.endswith(".pdf")):
            candidates.append(recorded_url)
        # citation_pdf_url — Google-Scholar metadata standard (first, but not
        # trusted exclusively).
        candidates += page.eval_on_selector_all(
            "meta[name='citation_pdf_url']", "els => els.map(e => e.content)")
        for selector in PDF_SELECTORS:
            candidates += page.eval_on_selector_all(
                selector, "els => els.map(e => e.href)")  # .href is absolute
        seen, ordered = set(), []
        for c in candidates:
            if c and c not in seen:
                seen.add(c)
                ordered.append(c)
        if ordered or time.monotonic() >= deadline:
            return ordered
        page.wait_for_timeout(1000)


_INPAGE_FETCH_JS = """async url => {
    const r = await fetch(url, {credentials: 'include'});
    if (!r.ok) return {status: r.status};
    const buf = await r.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let bin = '';
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk)
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    return {status: 200, b64: btoa(bin)};
}"""


def fetch_pdf_bytes(page, pdf_url: str) -> tuple[bytes | None, str | None]:
    """(pdf_bytes, error) — try the shared-cookie API request context first; on a
    WAF rejection fall back to an in-page fetch(), which carries the real
    browser's TLS/HTTP fingerprint (Silverchair 403s the API context even when
    the page itself cleared Cloudflare)."""
    api_err = None
    try:
        resp = page.request.get(pdf_url, timeout=60_000)
        if resp.status == 200:
            body = resp.body()
            if body.startswith(b"%PDF-"):
                return body, None
            api_err = "got HTML/other, not PDF"
        else:
            api_err = f"HTTP {resp.status}"
    except Exception as e:
        api_err = f"{type(e).__name__}: {e}"

    fetch_err = None
    try:
        import base64
        res = page.evaluate(_INPAGE_FETCH_JS, pdf_url)
        if res.get("b64"):
            body = base64.b64decode(res["b64"])
            if body.startswith(b"%PDF-"):
                return body, None
            fetch_err = "in-page fetch: got HTML/other"
        else:
            fetch_err = f"in-page fetch: HTTP {res.get('status')}"
    except Exception as e:
        fetch_err = f"in-page fetch: {type(e).__name__}: {str(e)[:50]}"

    # 3) navigation download — navigating to the PDF is a navigation-class
    #    request (real fingerprint, sec-fetch navigate), which WAFs treat like
    #    a user click; verified to return 200 where (1) and (2) get 403.
    #    Requires the profile preference plugins.always_open_pdf_externally
    #    (written at launch) so Chrome downloads instead of rendering the PDF.
    landing_url = page.url
    try:
        with page.expect_download(timeout=45_000) as dl:
            try:
                page.goto(pdf_url, timeout=45_000, wait_until="domcontentloaded")
            except Exception:
                pass  # navigation aborts when the response becomes a download
        body = Path(dl.value.path()).read_bytes()
        if body.startswith(b"%PDF-"):
            return body, None
        return None, f"{api_err}; {fetch_err}; nav download: not a PDF"
    except Exception as e:
        return None, (f"{api_err}; {fetch_err}; "
                      f"nav download: {type(e).__name__}: {str(e)[:50]}")
    finally:
        # restore the landing page so later candidates/flows still see it
        try:
            if page.url != landing_url:
                page.goto(landing_url, timeout=30_000, wait_until="domcontentloaded")
        except Exception:
            pass


def fetch_via_epdf_viewer(page) -> tuple[bytes | None, str | None, str | None]:
    """Wiley/AGU (ReadCube) ePDF flow. The real PDF lives at
    /doi/pdfdirect/...?download=true, which 403s when fetched cold — but works
    from *inside* the ePDF viewer session (which sets the needed cookies/referer).
    So: click the in-page PDF control (a.pdf-download) to open the viewer, then
    click its download button (a.navbar-download) and capture the download.
    Returns (pdf_bytes, url, error). ('no ePDF control' means this isn't a Wiley
    viewer page — not a real failure.)"""
    try:
        controls = page.locator("a.pdf-download")
        opened = False
        for i in range(controls.count()):
            if controls.nth(i).is_visible():
                controls.nth(i).click()
                opened = True
                break
        if not opened:
            return None, None, "no ePDF control"
        page.wait_for_selector("a.navbar-download", timeout=30_000)
        page.wait_for_timeout(1500)
        url = page.get_attribute("a.navbar-download", "href")
        with page.expect_download(timeout=60_000) as dl:
            page.click("a.navbar-download")
        return Path(dl.value.path()).read_bytes(), url, None
    except Exception as e:
        return None, None, f"epdf: {type(e).__name__}: {str(e)[:70]}"


def fetch_via_download_button(page) -> tuple[bytes | None, str | None, str | None]:
    """JS-triggered download flow (e.g. JKAS `jkas.kas.org`). The 'Download PDF'
    control has no plain href — it runs `javascript:downloadPDF('<token>')`, which
    fetches the PDF from a backend (koreascholar). Click it and capture the
    download. Returns (pdf_bytes, url, error). ('no download button' → not this
    kind of page.)"""
    try:
        btn = page.locator(
            "a:has-text('Download PDF'), a[href^='javascript:downloadPDF']")
        target = None
        for i in range(btn.count()):
            if btn.nth(i).is_visible():
                target = btn.nth(i)
                break
        if target is None:
            return None, None, "no download button"
        with page.expect_download(timeout=60_000) as dl:
            target.click()
        return Path(dl.value.path()).read_bytes(), dl.value.url, None
    except Exception as e:
        return None, None, f"dlbutton: {type(e).__name__}: {str(e)[:70]}"


def goto_landing(page, doi: str) -> tuple[bool, bytes | None, str | None]:
    """Navigate to the DOI landing page. Some DOIs redirect straight to a PDF
    download (Content-Disposition attachment), which aborts navigation — catch
    that as a download. Returns (reached_page, pdf_bytes_if_downloaded, error)."""
    try:
        with page.expect_download(timeout=8_000) as dl:
            page.goto(f"https://doi.org/{doi}", timeout=60_000,
                      wait_until="domcontentloaded")
        return False, Path(dl.value.path()).read_bytes(), None
    except PlaywrightTimeout:
        pass  # no download — normal landing page
    except Exception as e:
        # ERR_ABORTED without a captured download, or other nav error
        return False, None, f"landing: {type(e).__name__}: {str(e)[:80]}"
    page.wait_for_timeout(3000)
    wait_out_challenge(page)
    return True, None, None


def process(page, paper: dict, entry: dict, manifest: dict) -> bool:
    bc = paper["bibcode"]
    target = PDF_DIR / f"{paper['safe_bibcode']}.pdf"

    reached, dl_bytes, error = goto_landing(page, paper["doi"])

    def save(body: bytes, url: str | None) -> bool:
        part = target.with_suffix(".pdf.part")
        part.write_bytes(body)
        os.replace(part, target)
        fetch_pdfs.record(manifest, paper, status="ok", source="browser",
                          url=url, http_status=200,
                          sha256=fetch_pdfs.sha256_of(target),
                          size_bytes=target.stat().st_size, error=None)
        print(f"  OK {bc} ({target.stat().st_size:,} bytes)")
        return True

    def fail(msg: str, url: str | None = None) -> bool:
        fetch_pdfs.record(manifest, paper, status="failed", source="browser",
                          url=url, http_status=None, sha256=None,
                          size_bytes=None, error=msg)
        print(f"  FAILED {bc}: {msg}")
        return False

    # DOI redirected straight to a downloadable PDF (e.g. some JKAS)
    if dl_bytes is not None:
        if dl_bytes.startswith(b"%PDF-") and len(dl_bytes) >= fetch_pdfs.MIN_PDF_BYTES:
            return save(dl_bytes, f"https://doi.org/{paper['doi']}")
        return fail(f"DOI download not a valid PDF ({len(dl_bytes)} bytes)")
    if not reached:
        return fail(error or "could not reach landing page")

    errors = []

    # 1. ePDF-viewer flow (Wiley/AGU) FIRST, when the page has one. The viewer's
    #    download yields the article-specific PDF (pdfdirect); trying generic
    #    candidate links first risks grabbing a linked data document instead
    #    (the JGRA wrong-file case). No-op ("no ePDF control") on other publishers.
    body, url, eerr = fetch_via_epdf_viewer(page)
    if body and body.startswith(b"%PDF-") and len(body) >= fetch_pdfs.MIN_PDF_BYTES:
        return save(body, url)
    if eerr and eerr != "no ePDF control":
        errors.append(eerr)

    # 2. direct candidate PDF URLs via the browser's request context
    for url in find_pdf_urls(page, entry.get("url")):
        body, err = fetch_pdf_bytes(page, url)
        if body and len(body) >= fetch_pdfs.MIN_PDF_BYTES:
            return save(body, url)
        errors.append(f"{url.split('/')[2]}: {err}")

    # 3. JS-triggered "Download PDF" button (e.g. JKAS jkas.kas.org).
    body, url, derr = fetch_via_download_button(page)
    if body and body.startswith(b"%PDF-") and len(body) >= fetch_pdfs.MIN_PDF_BYTES:
        return save(body, url)
    if derr and derr != "no download button":
        errors.append(derr)

    return fail("; ".join(errors[:3]) if errors else "no PDF link found", page.url)
def main() -> None:
    parser = argparse.ArgumentParser(description="Browser-based retry for bot-blocked PDFs.")
    parser.add_argument("--only", metavar="BIBCODE")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--all-failed", action="store_true",
                        help="Retry every failed entry, not just HTTP 403")
    parser.add_argument("--headless", action="store_true",
                        help="Headless mode (Cloudflare blocks it — debugging only)")
    parser.add_argument("--sleep", type=float, default=4.0)
    args = parser.parse_args()

    manifest = fetch_pdfs.load_manifest()
    papers = {p["bibcode"]: p for p in load_papers()}

    todo = []
    for bc, entry in manifest.items():
        if entry["status"] != "failed":
            continue
        if args.only and bc != args.only:
            continue
        if not args.only and not args.all_failed and entry.get("error") != "HTTP 403":
            continue
        todo.append((papers[bc], entry))
    todo.sort(key=lambda t: t[0]["bibcode"])
    if args.limit:
        todo = todo[:args.limit]

    print(f"Browser retry targets: {len(todo)}")
    if not todo:
        return

    recovered = 0
    with sync_playwright() as p, tempfile.TemporaryDirectory(prefix="kb-chrome-") as profile:
        # Make PDF navigations download instead of rendering in the viewer —
        # fetch_pdf_bytes fallback (3) captures them via expect_download.
        prefs = Path(profile) / "Default"
        prefs.mkdir(parents=True, exist_ok=True)
        (prefs / "Preferences").write_text(
            '{"plugins": {"always_open_pdf_externally": true}}')

        # KB_PROXY (e.g. socks5://127.0.0.1:1080) routes the browser through a
        # tunnel into the institute network for publisher IP-range access.
        proxy_url = os.environ.get("KB_PROXY")
        if proxy_url:
            print(f"Using proxy: {proxy_url}")
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile,
            executable_path=CHROME_PATH,
            headless=args.headless,
            accept_downloads=True,
            proxy={"server": proxy_url} if proxy_url else None,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        for i, (paper, entry) in enumerate(todo):
            print(f"[{i + 1}/{len(todo)}] {paper['bibcode']}")
            recovered += process(page, paper, entry, manifest)
            if i < len(todo) - 1:
                time.sleep(args.sleep)
        context.close()

    ok = sum(1 for e in manifest.values() if e["status"] == "ok")
    failed = sum(1 for e in manifest.values() if e["status"] == "failed")
    print(f"\nRecovered this run: {recovered}/{len(todo)}")
    print(f"Overall: {ok} ok / {failed} failed of {len(papers)}")


if __name__ == "__main__":
    main()
