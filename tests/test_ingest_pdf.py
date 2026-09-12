from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from papertrail.config import CorpusConfigError, load_settings
from papertrail.pipeline import ingest_pdf


def test_ingest_bootstraps_manifest_sections_and_atomic_index(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    pdf = tmp_path / "draft.pdf"
    pdf.write_bytes(b"%PDF-test")
    assert ingest_pdf.initialize_corpus(corpus, pdf)
    fixture = Path(__file__).parent / "fixtures" / "marker" / "simple.json"
    marker = json.loads(fixture.read_text(encoding="utf-8"))
    calls = []

    def fake_convert(path, api_key, *, mode):
        calls.append((path, mode))
        return marker

    monkeypatch.setattr(ingest_pdf, "convert_pdf", fake_convert)
    args = argparse.Namespace(
        pdf=pdf, corpus=str(corpus), doc_id="draft", title="Draft Paper",
        authors=["A. Author"], year=2026, journal="Journal", fulltext_source="author_draft",
        mode="balanced", force=False,
    )
    settings = load_settings(corpus, create_dirs=True)

    first = ingest_pdf.run(settings, args)
    second = ingest_pdf.run(settings, args)

    assert first["node_count"] > 0
    assert not first["reused"]
    assert second["reused"]
    assert len(calls) == 1
    assert (settings.corpus.marker_dir() / "draft.json").exists()
    assert (settings.sections_dir / "draft.json").exists()
    assert (corpus / "documents.yaml").exists()
    papers = settings.corpus.load_papers()
    assert papers[0]["title"] == "Draft Paper"
    with sqlite3.connect(settings.sqlite_path) as db:
        assert db.execute("SELECT title FROM docs WHERE doc_id='draft'").fetchone()[0] == "Draft Paper"
        assert db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] > 0


def test_initialize_corpus_uses_title_override_and_keeps_other_files(tmp_path):
    corpus = tmp_path / "existing-dir"
    corpus.mkdir()
    keep = corpus / "keep.txt"
    keep.write_text("untouched", encoding="utf-8")

    created = ingest_pdf.initialize_corpus(
        corpus, tmp_path / "paper.pdf", "Custom Reader")
    config = yaml.safe_load((corpus / "corpus.yaml").read_text(encoding="utf-8"))

    assert created
    assert config["corpus"] == {"name": "existing-dir", "title": "Custom Reader"}
    assert config["source"]["documents_file"] == "documents.yaml"
    assert config["identity"]["citation_style"] == "plain"
    assert keep.read_text(encoding="utf-8") == "untouched"


def test_initialize_corpus_never_overwrites_existing_yaml(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    path = corpus / "corpus.yaml"
    path.write_text("custom: true\n", encoding="utf-8")

    assert not ingest_pdf.initialize_corpus(corpus, tmp_path / "paper.pdf")
    assert path.read_text(encoding="utf-8") == "custom: true\n"


def test_missing_corpus_still_fails_without_init_flag(tmp_path):
    with pytest.raises(CorpusConfigError, match="no corpus.yaml"):
        ingest_pdf.main([str(tmp_path / "paper.pdf"), "--corpus", str(tmp_path / "missing")])


def test_corpus_title_requires_init_flag(tmp_path):
    with pytest.raises(SystemExit) as exc:
        ingest_pdf.main([
            str(tmp_path / "paper.pdf"), "--corpus", str(tmp_path / "corpus"),
            "--corpus-title", "Reader",
        ])
    assert exc.value.code == 2
