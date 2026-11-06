"""Ingest one EPUB: EPUB -> Datalab-shaped JSON -> section tree -> SQLite index.

The EPUB counterpart of `ingest-pdf`. The conversion runs locally
(`papertrail.epub`, no API key, no network) and is written to the same
<marker_dir>/<doc_id>.json slot a PDF conversion uses, so `papertrail sections`,
`index`, `serve` and `mcp` treat both formats alike. Ingesting an EPUB under a
doc_id that was previously ingested from a PDF replaces the PDF-derived tree.

Bibliographic fields not given on the command line, and not already in
documents.yaml, are filled from the EPUB's own OPF metadata (title, authors,
year, journal, DOI).
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from ..config import load_settings
from ..epub import CONVERTER_VERSION, convert_epub, is_epub
from . import sections
from .ingest_pdf import (_SAFE_DOC_ID, _atomic_json, _atomic_rebuild_index,
                         _upsert_document, initialize_corpus, sha256_of)

# EDP Sciences titles carry a citation prefix: "A&A, 685, A75 (2024): Title".
_CITATION_PREFIX = re.compile(r"^(?P<cite>[^:]{2,80}?\(\d{4}\)):\s*(?P<title>.+)$")


def metadata_defaults(meta: dict) -> dict:
    """documents.yaml defaults from `convert_epub()`'s `epub_metadata`."""
    out: dict = {"extra": {}}
    title = meta.get("title")
    if title:
        m = _CITATION_PREFIX.match(title)
        if m:
            title = m.group("title")
            # "A&A, 685, A75 (2024)" / "A&A 675, A111 (2023)" -> "A&A"
            out["journal"] = re.match(r"[^,\d]*", m.group("cite")).group(0).strip() or None
        out["title"] = title
    if meta.get("authors"):
        out["authors"] = meta["authors"]
    if meta.get("year"):
        out["year"] = meta["year"]
    for key in ("doi", "bibcode", "isbn"):
        if meta.get(key):
            out["extra"][key] = meta[key]
    return out


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert one EPUB locally, build sections, and refresh the index.")
    parser.add_argument("epub", type=Path)
    parser.add_argument("--corpus", required=True, metavar="PATH")
    parser.add_argument("--init-corpus", action="store_true",
                        help="create a minimal corpus.yaml when it is missing")
    parser.add_argument("--corpus-title",
                        help="browser title for a corpus created by --init-corpus")
    parser.add_argument("--doc-id")
    parser.add_argument("--title")
    parser.add_argument("--authors", nargs="+")
    parser.add_argument("--year", type=int)
    parser.add_argument("--journal")
    parser.add_argument("--fulltext-source", default="publisher_epub")
    parser.add_argument("--force", action="store_true",
                        help="reconvert even if this EPUB was already converted")
    return parser


def run(settings, args) -> dict:
    epub = args.epub.resolve()
    if not epub.is_file() or not is_epub(epub):
        raise ValueError(f"not a readable EPUB: {epub}")
    doc_id = args.doc_id or epub.stem
    if not _SAFE_DOC_ID.fullmatch(doc_id):
        raise ValueError("doc_id may contain only letters, digits, '.', '_', '+', and '-'")

    marker_path = settings.corpus.marker_dir() / f"{doc_id}.json"
    manifest_path = settings.data_dir / "ingest_manifest.json"
    manifest = (json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest_path.exists() else {})
    epub_sha = sha256_of(epub)
    previous = manifest.get(doc_id) or {}
    reused = (not args.force and marker_path.exists()
              and previous.get("status") == "complete"
              and previous.get("source_format") == "epub"
              and previous.get("converter") == CONVERTER_VERSION
              and previous.get("sha256_of_source") == epub_sha)

    if reused:
        result = json.loads(marker_path.read_text(encoding="utf-8"))
        print(f"EPUB: unchanged, reusing {marker_path}")
    else:
        print(f"EPUB: converting {epub.name}")
        result = convert_epub(epub)
        _atomic_json(marker_path, result)
        manifest[doc_id] = {
            "status": "complete",
            "source_format": "epub",
            "converter": CONVERTER_VERSION,
            "page_count": None,
            "sha256_of_source": epub_sha,
        }
        _atomic_json(manifest_path, manifest)

    _upsert_document(settings, doc_id, result, args,
                     defaults=metadata_defaults(result.get("epub_metadata") or {}))
    stats = sections.process_one(settings, doc_id, marker_path)
    _atomic_rebuild_index(settings)
    print(f"Ready: {doc_id} — {stats['node_count']} nodes, {stats['ref_count']} references")
    return {"doc_id": doc_id, "marker_path": marker_path, **stats, "reused": reused}


def main(argv=None) -> None:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.corpus_title and not args.init_corpus:
        parser.error("--corpus-title requires --init-corpus")
    if args.init_corpus:
        initialize_corpus(Path(args.corpus), args.epub, args.corpus_title)
    settings = load_settings(args.corpus, create_dirs=True)
    run(settings, args)


if __name__ == "__main__":
    main()
