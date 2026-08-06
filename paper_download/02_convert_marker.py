#!/usr/bin/env python3
"""Step 2 — Convert downloaded PDFs to structured JSON via Datalab Marker.

POST https://www.datalab.to/api/v1/convert with output_format=json, then poll
request_check_url until complete. The full final response (with the parsed
block tree under "json") is saved to paper_download/data/marker/<safe_bibcode>.json.

Resumable: a PDF is skipped when its marker JSON exists with status=complete
AND the PDF's sha256 matches the manifest (so a manually replaced PDF is
reconverted automatically). Image extraction is disabled — text-only output;
captions still come through as Caption blocks.

Usage:
    python3 02_convert_marker.py                 # convert all downloaded PDFs
    python3 02_convert_marker.py --mode accurate --only 2021SpWea..1902741K
    python3 02_convert_marker.py --dry-run
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pikepdf
import requests
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent))
from config import DATALAB_API_KEY, MARKER_DIR, MARKER_MANIFEST, PDF_DIR, load_papers

CONVERT_URL = "https://www.datalab.to/api/v1/convert"
POLL_INTERVAL = 2
POLL_TIMEOUT = 15 * 60
RETRIES = 2

_manifest_lock = threading.Lock()


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest() -> dict:
    if MARKER_MANIFEST.exists():
        return json.loads(MARKER_MANIFEST.read_text(encoding="utf-8"))
    return {}


def save_manifest(manifest: dict) -> None:
    tmp = MARKER_MANIFEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, MARKER_MANIFEST)


def sanitize_for_upload(pdf_path: Path, tmp_path: Path) -> None:
    """Strip /OpenAction (and other auto-run action dicts) from the catalog
    before upload. Some publisher PDF pipelines (seen once, on an ApJ PDF)
    leave a benign /OpenAction [page /FitH ...] entry to auto-fit the initial
    view — Cloudflare's WAF in front of the Datalab API blocks the upload on
    that byte pattern regardless (it's a classic PDF-exploit signature,
    false-positiving here). The archived PDF in data/pdfs/ is never touched —
    this writes a throwaway sanitized copy just for the upload."""
    pdf = pikepdf.open(pdf_path)
    try:
        for key in ("/OpenAction", "/AA"):
            if key in pdf.Root:
                del pdf.Root[key]
        pdf.save(tmp_path)
    finally:
        pdf.close()


def convert_one(pdf_path: Path, mode: str) -> dict:
    """Submit one PDF and poll to completion. Returns the final response dict."""
    headers = {"X-API-Key": DATALAB_API_KEY}
    with tempfile.TemporaryDirectory() as tmp_dir:
        upload_path = Path(tmp_dir) / "upload.pdf"
        try:
            sanitize_for_upload(pdf_path, upload_path)
        except Exception:
            upload_path = pdf_path  # pikepdf choked — fall back to the raw file
        with open(upload_path, "rb") as fh:
            resp = requests.post(
                CONVERT_URL,
                headers=headers,
                files={"file": ("paper.pdf", fh, "application/pdf")},
                data={"output_format": "json", "mode": mode,
                      "disable_image_extraction": "true", "paginate": "false"},
                timeout=120,
            )
    resp.raise_for_status()
    submitted = resp.json()
    check_url = submitted["request_check_url"]

    deadline = time.monotonic() + POLL_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        poll = requests.get(check_url, headers=headers, timeout=60)
        poll.raise_for_status()
        result = poll.json()
        if result.get("status") in ("complete", "failed"):
            return result
    raise TimeoutError(f"polling timed out after {POLL_TIMEOUT}s")


def process_paper(paper: dict, manifest: dict, mode: str) -> tuple[str, str]:
    """Convert one paper's PDF. Returns (bibcode, outcome)."""
    bc = paper["bibcode"]
    safe = paper["safe_bibcode"]
    pdf_path = PDF_DIR / f"{safe}.pdf"
    out_path = MARKER_DIR / f"{safe}.json"
    pdf_sha = sha256_of(pdf_path)

    last_error = None
    for attempt in range(RETRIES + 1):
        try:
            result = convert_one(pdf_path, mode)
            if result.get("status") == "complete" and result.get("success", True):
                # The block tree may arrive as a serialized string — normalize.
                if isinstance(result.get("json"), str):
                    result["json"] = json.loads(result["json"])
                tmp = out_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(result, ensure_ascii=False),
                               encoding="utf-8")
                os.replace(tmp, out_path)
                entry = {
                    "status": "complete",
                    "mode": mode,
                    "page_count": result.get("page_count"),
                    "parse_quality_score": result.get("parse_quality_score"),
                    "sha256_of_pdf": pdf_sha,
                    "converted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "error": None,
                }
                with _manifest_lock:
                    manifest[bc] = entry
                    save_manifest(manifest)
                return bc, "ok"
            last_error = result.get("error") or f"status={result.get('status')}"
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
        if attempt < RETRIES:
            time.sleep(5 * (attempt + 1))

    with _manifest_lock:
        manifest[bc] = {
            "status": "failed", "mode": mode, "page_count": None,
            "parse_quality_score": None, "sha256_of_pdf": pdf_sha,
            "converted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error": last_error,
        }
        save_manifest(manifest)
    return bc, f"failed: {last_error}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert PDFs to JSON via Datalab Marker.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Reconvert even if JSON exists")
    parser.add_argument("--only", metavar="BIBCODE")
    parser.add_argument("--mode", choices=["fast", "balanced", "accurate"], default="balanced")
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    if not DATALAB_API_KEY:
        sys.exit("DATALAB_API_KEY not found in .env")

    papers = load_papers()
    manifest = load_manifest()

    todo, no_pdf, done = [], 0, 0
    for paper in papers:
        bc = paper["bibcode"]
        if args.only and bc != args.only:
            continue
        pdf_path = PDF_DIR / f"{paper['safe_bibcode']}.pdf"
        if not pdf_path.exists():
            no_pdf += 1
            continue
        out_path = MARKER_DIR / f"{paper['safe_bibcode']}.json"
        entry = manifest.get(bc)
        if (not args.force and out_path.exists()
                and entry and entry["status"] == "complete"
                and entry.get("sha256_of_pdf") == sha256_of(pdf_path)):
            done += 1
            continue
        todo.append(paper)
    if args.limit:
        todo = todo[:args.limit]

    print(f"Papers: {len(papers)} | already converted: {done} | "
          f"no PDF yet: {no_pdf} | to convert: {len(todo)}")
    if args.dry_run or not todo:
        for p in todo:
            print(f"  [dry-run] {p['bibcode']}")
        if not args.dry_run:
            print_quality_report(manifest)
        return

    print(f"Mode: {args.mode} | workers: {args.workers}")
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_paper, p, manifest, args.mode): p["bibcode"]
                   for p in todo}
        with tqdm(total=len(futures), desc="Converting") as pbar:
            for future in as_completed(futures):
                bc, outcome = future.result()
                if outcome != "ok":
                    tqdm.write(f"  {bc}: {outcome}")
                pbar.update(1)

    print_quality_report(manifest)


def print_quality_report(manifest: dict) -> None:
    complete = {bc: e for bc, e in manifest.items() if e["status"] == "complete"}
    failed = {bc: e for bc, e in manifest.items() if e["status"] == "failed"}
    pages = sum(e.get("page_count") or 0 for e in complete.values())
    print(f"\nConverted: {len(complete)} ({pages} pages total) | failed: {len(failed)}")
    for bc, e in sorted(failed.items()):
        print(f"  FAILED {bc}: {e['error']}")
    suspicious = [(bc, e) for bc, e in complete.items()
                  if (e.get("parse_quality_score") or 5) < 3
                  or (e.get("page_count") or 0) > 60]
    if suspicious:
        print("\nSuspicious conversions (quality < 3 or > 60 pages) — eyeball these:")
        for bc, e in sorted(suspicious):
            print(f"  {bc}: quality={e.get('parse_quality_score')} pages={e.get('page_count')}")


if __name__ == "__main__":
    main()
