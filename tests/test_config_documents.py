from __future__ import annotations

import pytest
import yaml

from papertrail.config import CorpusConfigError, load_settings


def _write_corpus(tmp_path, source):
    (tmp_path / "corpus.yaml").write_text(
        yaml.safe_dump({"corpus": {"name": "test"}, "source": source}),
        encoding="utf-8",
    )


def test_documents_file_loads_without_python_adapter(tmp_path):
    _write_corpus(tmp_path, {"marker_dir": "data/marker", "documents_file": "documents.yaml"})
    (tmp_path / "documents.yaml").write_text(
        yaml.safe_dump({"documents": [{"doc_id": "paper-1", "title": "One"}]}),
        encoding="utf-8",
    )

    papers = load_settings(tmp_path).corpus.load_papers()

    assert papers == [{"doc_id": "paper-1", "title": "One"}]


def test_documents_file_and_adapter_are_mutually_exclusive(tmp_path):
    _write_corpus(tmp_path, {
        "marker_dir": "data/marker",
        "documents_file": "documents.yaml",
        "metadata_adapter": "anything:load",
    })
    (tmp_path / "documents.yaml").write_text("documents: []\n", encoding="utf-8")

    with pytest.raises(CorpusConfigError, match="mutually exclusive"):
        load_settings(tmp_path).corpus.load_papers()


def test_documents_file_requires_doc_ids(tmp_path):
    _write_corpus(tmp_path, {"marker_dir": "data/marker", "documents_file": "documents.yaml"})
    (tmp_path / "documents.yaml").write_text("documents:\n  - title: Missing ID\n", encoding="utf-8")

    with pytest.raises(CorpusConfigError, match="doc_id"):
        load_settings(tmp_path).corpus.load_papers()
