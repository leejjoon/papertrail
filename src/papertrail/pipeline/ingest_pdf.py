"""Ingest one arbitrary PDF: Datalab JSON -> section tree -> SQLite index."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from dataclasses import replace
from pathlib import Path

import yaml

from ..config import CorpusConfigError, Settings, load_settings
from ..datalab import convert_pdf
from . import index, sections

_SAFE_DOC_ID = re.compile(r"^[A-Za-z0-9._+-]+$")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def initialize_corpus(corpus_root: Path, pdf: Path, corpus_title: str | None = None) -> bool:
    """Create a minimal manifest-backed corpus without overwriting one.

    Returns True when corpus.yaml was created and False when it already
    existed.  The temporary file is hard-linked into place so the target
    appears atomically and a concurrent/existing configuration cannot be
    replaced accidentally.
    """
    root = corpus_root.resolve()
    corpus_yaml = root / "corpus.yaml"
    if corpus_yaml.exists():
        return False
    root.mkdir(parents=True, exist_ok=True)
    config = {
        "corpus": {
            "name": root.name,
            "title": corpus_title or f"{pdf.stem} Paper Reader",
        },
        "source": {
            "marker_dir": "data/marker",
            "documents_file": "documents.yaml",
        },
        "identity": {"citation_style": "plain"},
        "navigation": {"year_pages": False},
        "wiki_headings": [],
    }
    text = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    tmp = corpus_yaml.with_name(f".{corpus_yaml.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        try:
            os.link(tmp, corpus_yaml)
        except FileExistsError:
            return False
    finally:
        tmp.unlink(missing_ok=True)
    print(f"Initialized corpus: {corpus_yaml}")
    return True


def _first_heading(document: dict) -> str | None:
    def walk(nodes):
        for node in nodes or []:
            if node.get("block_type") == "SectionHeader":
                text = re.sub(r"<[^>]+>", " ", node.get("html") or "")
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    return text
            found = walk(node.get("children"))
            if found:
                return found
        return None
    return walk(document.get("children"))


def _upsert_document(settings: Settings, doc_id: str, result: dict, args) -> None:
    path = settings.corpus.documents_path()
    if path is None:
        known = {p["doc_id"] for p in settings.corpus.load_papers()}
        if doc_id not in known:
            raise CorpusConfigError(
                f"{doc_id!r} is not returned by this corpus's metadata adapter")
        return

    parsed = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    if parsed is None:
        parsed = {}
    documents = parsed.get("documents", []) if isinstance(parsed, dict) else parsed
    if not isinstance(documents, list):
        raise CorpusConfigError(f"documents file must contain a documents list: {path}")
    existing = next((d for d in documents if d.get("doc_id") == doc_id), {})
    entry = {
        **existing,
        "doc_id": doc_id,
        "title": args.title or existing.get("title") or _first_heading(result["json"]) or doc_id,
        "authors": args.authors if args.authors is not None else existing.get("authors"),
        "year": args.year if args.year is not None else existing.get("year"),
        "journal": args.journal if args.journal is not None else existing.get("journal"),
        "has_fulltext": True,
        "fulltext_source": args.fulltext_source,
        "extra": existing.get("extra") or {},
    }
    if existing:
        documents[documents.index(existing)] = entry
    else:
        documents.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump({"documents": documents}, sort_keys=False, allow_unicode=True),
                   encoding="utf-8")
    os.replace(tmp, path)


def _atomic_rebuild_index(settings: Settings) -> None:
    target = settings.sqlite_path
    tmp = target.with_suffix(target.suffix + ".building")
    if tmp.exists():
        tmp.unlink()
    if target.exists():
        shutil.copy2(target, tmp)
    try:
        index.run(replace(settings, sqlite_path=tmp))
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert one PDF with Datalab, build sections, and refresh the index.")
    parser.add_argument("pdf", type=Path)
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
    parser.add_argument("--fulltext-source", default="author_draft")
    parser.add_argument("--mode", choices=("fast", "balanced", "accurate"), default="balanced")
    parser.add_argument("--force", action="store_true")
    return parser


def run(settings: Settings, args) -> dict:
    pdf = args.pdf.resolve()
    if not pdf.is_file():
        raise ValueError(f"not a readable PDF: {pdf}")
    with pdf.open("rb") as fh:
        magic = fh.read(5)
    if magic != b"%PDF-":
        raise ValueError(f"not a readable PDF: {pdf}")
    doc_id = args.doc_id or pdf.stem
    if not _SAFE_DOC_ID.fullmatch(doc_id):
        raise ValueError("doc_id may contain only letters, digits, '.', '_', '+', and '-'")

    marker_dir = settings.corpus.marker_dir()
    marker_path = marker_dir / f"{doc_id}.json"
    manifest_path = settings.data_dir / "ingest_manifest.json"
    manifest = (json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest_path.exists() else {})
    pdf_sha = sha256_of(pdf)
    previous = manifest.get(doc_id) or {}
    reused = (not args.force and marker_path.exists()
              and previous.get("status") == "complete"
              and previous.get("sha256_of_pdf") == pdf_sha)

    if reused:
        result = json.loads(marker_path.read_text(encoding="utf-8"))
        print(f"Datalab: unchanged PDF, reusing {marker_path}")
    else:
        print(f"Datalab: converting {pdf.name} ({args.mode} mode)")
        result = convert_pdf(pdf, os.getenv("DATALAB_API_KEY", ""), mode=args.mode)
        _atomic_json(marker_path, result)
        manifest[doc_id] = {
            "status": "complete",
            "mode": args.mode,
            "page_count": result.get("page_count"),
            "parse_quality_score": result.get("parse_quality_score"),
            "sha256_of_pdf": pdf_sha,
        }
        _atomic_json(manifest_path, manifest)

    _upsert_document(settings, doc_id, result, args)
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
        initialize_corpus(Path(args.corpus), args.pdf, args.corpus_title)
    settings = load_settings(args.corpus, create_dirs=True)
    run(settings, args)


if __name__ == "__main__":
    main()
