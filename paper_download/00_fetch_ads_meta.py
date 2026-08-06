#!/usr/bin/env python3
"""Step 0 — Resolve DOI + esources for every fetchable paper, once.

wasp_12b_papers.yaml carries a bibcode but no DOI, and 01_fetch_pdfs.py /
01b_fetch_pdfs_browser.py both need one (DOI landing-page fallback in 01,
every navigation in 01b). This does one batched ADS query per 100 bibcodes
and caches {bibcode: {doi, esources}} to data/ads_meta.json, which
config.load_papers() reads to populate paper["doi"] / paper["esources"].

Idempotent — re-run to refresh (e.g. after adding papers to the YAML).

Usage:
    python3 00_fetch_ads_meta.py
"""
import json
import sys
import time
from pathlib import Path

import requests
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent))
from config import ADS_API_TOKEN, ADS_META, load_papers

ADS_SEARCH_URL = "https://api.adsabs.harvard.edu/v1/search/query"
BATCH_SIZE = 100


def fetch_meta(bibcodes: list[str], retries: int = 3) -> dict:
    headers = {"Authorization": f"Bearer {ADS_API_TOKEN}"}
    results: dict[str, dict] = {}
    batches = [bibcodes[i:i + BATCH_SIZE] for i in range(0, len(bibcodes), BATCH_SIZE)]
    for batch in tqdm(batches, unit="batch", desc="ADS meta"):
        params = {
            "q": "bibcode:(" + " OR ".join(batch) + ")",
            "fl": "bibcode,doi,esources",
            "rows": len(batch),
        }
        for attempt in range(retries):
            resp = requests.get(ADS_SEARCH_URL, headers=headers, params=params, timeout=30)
            if resp.status_code == 429:
                wait = 60 * (attempt + 1)
                tqdm.write(f"ADS rate limited — waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            for doc in resp.json().get("response", {}).get("docs", []):
                doi_list = doc.get("doi") or []
                results[doc["bibcode"]] = {
                    "doi": doi_list[0] if doi_list else None,
                    "esources": doc.get("esources", []),
                }
            break
        else:
            raise RuntimeError(f"ADS request failed after {retries} retries")
        if len(batches) > 1:
            time.sleep(0.3)
    return results


def main() -> None:
    if not ADS_API_TOKEN:
        sys.exit("ADS_API_TOKEN not found in .env")

    papers = load_papers(include_skipped=True)
    bibcodes = [p["bibcode"] for p in papers if p.get("bibcode") and not p.get("skip_reason")]
    print(f"Resolving DOI/esources for {len(bibcodes)} fetchable papers "
          f"({len(papers) - len(bibcodes)} skipped-by-design)...")

    meta = fetch_meta(bibcodes)
    ADS_META.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    no_doi = [bc for bc in bibcodes if not meta.get(bc, {}).get("doi")]
    print(f"Resolved: {len(meta)}/{len(bibcodes)}  |  no DOI found: {len(no_doi)}")
    if no_doi:
        print("No-DOI bibcodes (landing-page fallback in 01 won't work for these):")
        for bc in no_doi:
            print(f"  {bc}")


if __name__ == "__main__":
    main()
