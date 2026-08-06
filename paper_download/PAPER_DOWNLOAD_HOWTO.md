# How to Download a Paper PDF

A practical, action-oriented runbook for getting a published-version PDF onto disk —
whether you're running the full batch (`kb/01_fetch_pdfs.py` + `kb/01b_fetch_pdfs_browser.py`)
or chasing down one stubborn paper by hand. Covers the full cascade end to end: plain HTTP,
headful-browser Cloudflare bypass, the SOCKS tunnel for off-network access, the
Elsevier/ScienceDirect special case, and the manual fallback.

Scope: publisher-direct only. No arXiv, no Unpaywall, no sci-hub-likes — this fetches the
*published* version, using the institute's subscription access. It's meant to run on a
KASI-network machine (or tunneled to one, see §2).

---

## 0. Decision tree — start here

```
Got a bibcode or DOI to fetch?
│
├─ Batch (many papers)? ─────────────► go to §1 (the two scripts, in order)
│
└─ Just one paper? Try in order, stop at first success:
   1. `.venv/bin/python kb/01_fetch_pdfs.py --only <bibcode>`        (§1a)
   2. `xvfb-run -a .venv/bin/python kb/01b_fetch_pdfs_browser.py --only <bibcode>`  (§1b)
   3. Not on KASI network? Open the SOCKS tunnel first (§2), retry 1 and 2.
   4. Elsevier/ScienceDirect specifically? Skip straight to §3 (CDP + real Chrome).
   5. Still nothing? Manual browser download → drop the file (§4).
```

Every step is resumable and idempotent — re-running never re-downloads a file that's
already validated in `kb/data/pdf_manifest.json`.

---

## 1. The two-script cascade (does ~90% of the work)

### 1a. Plain-requests pass — `01_fetch_pdfs.py`

Tries, per paper: **ADS `PUB_PDF` link gateway** → **DOI landing-page scrape**
(`<meta name="citation_pdf_url">` + fallback link selectors). No browser, no display needed.

```bash
.venv/bin/python kb/01_fetch_pdfs.py --dry-run          # preview what's missing
.venv/bin/python kb/01_fetch_pdfs.py                     # fetch everything pending
.venv/bin/python kb/01_fetch_pdfs.py --retry-failed      # retry only status=failed
.venv/bin/python kb/01_fetch_pdfs.py --only <bibcode>    # one paper
.venv/bin/python kb/01_fetch_pdfs.py --limit 5           # first 5 pending only
```

Ends with a success/failure report grouped by journal. **Works cleanly for**: AAS/IOP
(ApJ/AJ/ApJS/ApJL), A&A, Springer (Solar Physics) — all fully open-access or unblocked
since 2022.

**Fails (403/HTML instead of PDF) for**: anything Cloudflare- or bot-gated — OUP/MNRAS,
APS/PhRvD, Wiley/AGU, most Korean-OA sites with SSL quirks. That's expected — move to 1b.

### 1b. Browser pass — `01b_fetch_pdfs_browser.py`

Headful Chrome via Playwright, run under a virtual display. Clears Cloudflare's "Just a
moment…" challenge and handles viewer-based download flows plain HTTP can't.

```bash
xvfb-run -a .venv/bin/python kb/01b_fetch_pdfs_browser.py --all-failed   # everything 01 couldn't get
xvfb-run -a .venv/bin/python kb/01b_fetch_pdfs_browser.py --only <bibcode>
xvfb-run -a .venv/bin/python kb/01b_fetch_pdfs_browser.py --limit 5
xvfb-run -a .venv/bin/python kb/01b_fetch_pdfs_browser.py --headless      # only if you know the site doesn't need headful (rare)
```

**On macOS or a machine with a real display**, drop `xvfb-run -a` — a visible Chrome window
will pop up and that's fine. **Headless Chrome will not clear Cloudflare** (it's fingerprint-
detected) — always run this headful, which is why `xvfb-run` exists for headless Linux boxes.

**Works for**: OUP/MNRAS (Silverchair), APS/PhRvD, AGU/Wiley (ePDF-viewer flow), most Korean
sites. **Still won't touch**: Elsevier/ScienceDirect — its anti-scraping JS challenge (CRA)
beats even headful Playwright. Go to §3.

---

## 2. Not on the KASI network? Tunnel to one

Publisher access is IP-gated. If you're running this off-site, open an SSH dynamic (SOCKS5)
tunnel to any KASI-network host and route both scripts through it:

```bash
ssh -f -N -D 1080 <user>@<kasi-host>

# plain-requests stage — needs pysocks (pip install pysocks); socks5h so DNS resolves remotely too
ALL_PROXY=socks5h://127.0.0.1:1080 .venv/bin/python kb/01_fetch_pdfs.py

# browser stage — route Chrome through the same tunnel; prefer real Google Chrome over Chromium
KB_PROXY=socks5://127.0.0.1:1080 xvfb-run -a .venv/bin/python kb/01b_fetch_pdfs_browser.py --all-failed
```

This alone fixes most captcha/geofencing issues (IOP/JCAP's Radware captcha, Springer, JKAS
all clear once the request looks like it's coming from campus). It does **not** fully fix
OUP/Silverchair or APS — those additionally reject anything that isn't a full page
navigation, even from the institutional IP. `01b`'s fetch logic already escalates through
three request styles to handle this — API request → in-page `fetch()` → navigation-as-a-
download (the browser profile sets `plugins.always_open_pdf_externally: true` so a PDF
navigation becomes a capturable download instead of opening in the built-in viewer, whose
response body a script can't read) — you just need the tunnel up so all three get a fair try.

`CHROME_PATH` env var overrides which browser binary `01b` launches, if you need a specific one.

---

## 3. Elsevier / ScienceDirect (the one that fights back)

Nothing scripted works here — not `requests`, not Playwright's API request context, not
in-page `fetch()`. ScienceDirect's challenge only lets go for a **real, human-launched**
Chrome doing a **trusted** navigation and a **trusted** click.

```bash
# 1. Launch real Chrome with a debug port, through the KASI tunnel if off-site:
google-chrome --remote-debugging-port=9222 \
  --user-data-dir=$HOME/.cache/chrome-devtools-mcp/chrome-profile \
  --proxy-server=socks5://127.0.0.1:1080 &

# 2. In that Chrome window: chrome://settings/content/pdfDocuments → "Download PDFs"
#    (NOT "Open PDFs in Chrome"). Required — otherwise the inline viewer intercepts
#    the download and setDownloadBehavior can't force it out.
```

Then, per paper, drive it over CDP (Playwright `connect_over_cdp` or similar):

1. `ctx.new_cdp_session(page)` → `Browser.setDownloadBehavior` with
   `{"behavior": "allowAndName", "downloadPath": <local dir>, "eventsEnabled": True}`.
2. `page.goto("https://doi.org/<doi>")`, wait for `a[href*='pdfft']` to appear.
3. `page.click("a[href*='pdfft']")` — must be a **real Playwright click** (trusted input
   event). A synthetic `element.click()` via `page.evaluate` is silently ignored.
4. Poll the download directory for the completed file (not `.crdownload`).

Both settings must hold — Chrome set to "Download PDFs" *and* a trusted click — or the
viewer intercepts it and nothing downloads. With both, each paper takes ~15s.

If even this fails (rare), it's genuinely manual — see §4.

---

## 4. Manual fallback (any publisher)

For the handful that never yield to automation: download it yourself in a browser with
institutional access, then drop it where the pipeline expects it:

```
kb/data/pdfs/<safe_bibcode>.pdf
```

- `safe_bibcode` = the ADS bibcode with `&` → `+` (filesystems dislike `&`).
- `kb/data/FAILED_PDFS.md` lists the exact filenames/bibcodes still outstanding, if that
  file exists in your checkout — otherwise check `kb/data/pdf_manifest.json` for any entry
  with `"status": "failed"`.
- Re-run `01_fetch_pdfs.py` afterward — it validates the file (starts with `%PDF-`, ≥20KB)
  and records it with `source: manual` in the manifest. Nothing else re-fetches a file
  that's already validated.
- Downstream stages (`02_convert_marker.py` onward) pick it up on their next run like any
  other successfully-fetched PDF.

---

## 5. Validate what you got

Every download is checked for `%PDF-` magic bytes and a ≥20KB minimum size — HTML
interstitials (login walls, cookie banners) get caught here, not silently accepted.

**Wrong-file risk**: a generic `.pdf` / `article-pdf` selector can occasionally match a
*linked* document instead of the actual paper (happened once: a JGRA DOI resolved to a
174-page NASA MLS data file instead of the article). `02_convert_marker.py` flags any
`page_count > 60` — always eyeball those outliers and confirm the title matches before
trusting the fulltext extraction.

---

## 6. Publisher quick-reference

| Publisher (DOI prefix) | Method | One-line note |
|---|---|---|
| AAS/IOP (`10.3847`, `10.1088`) | `01` (ADS gateway) | OA since 2022, no fuss |
| A&A (`10.1051`) | `01` (ADS gateway) | OA since 2022, no fuss |
| Springer (`10.1007`) | `01` (DOI landing) | `citation_pdf_url` works directly |
| OUP/Silverchair (`10.1093`) | `01b` (browser) | Cloudflare; needs headful + navigation-class fetch |
| APS (`10.1103`) | `01b` (browser) | Cloudflare; meta tag is a viewer, use on-page `journals.aps.org/.../pdf/...` link |
| Wiley/AGU (`10.1029`) | `01b` (browser, ePDF flow) | Must click into in-page viewer then its download button |
| Korean OA (`10.5303`/`10.5140`/`10.52912`) | `01` or `01b` | koreascience.or.kr; SSL quirks sometimes need the browser |
| Elsevier (`10.1016`) | §3 (CDP + real Chrome) or manual | Only trusted-click navigation clears the CRA challenge |
| arXiv-only (`10.48550`) | n/a | No published version to fetch |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `01` reports "not a PDF (got HTML/other)" | Expected for Cloudflare sites — try `01b`. |
| `01b` can't launch a browser | No `$DISPLAY`? Wrap in `xvfb-run -a` (Linux). macOS headless has no escape hatch — needs a real GUI session. |
| `01b` still 403s on OUP/APS | Confirm you're on/tunneled to the institutional IP (§2) — off-network fails even with the browser. |
| Downloaded PDF has `page_count > 60` and looks wrong | Probably a linked/data document, not the article — check the title, replace manually (§4). |
| Elsevier every method fails | Confirm Chrome's PDF setting is "Download PDFs" and the click is a real Playwright click, not `page.evaluate(...click())`. |
| Need to re-fetch a paper that's marked done | `--force` on `01`, or delete its `kb/data/pdfs/<safe_bibcode>.pdf` and its manifest entry, then re-run. |
