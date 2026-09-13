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


def test_agent_settings_default_to_chat_model_and_local_mcp(tmp_path, monkeypatch):
    _write_corpus(tmp_path, {"marker_dir": "data/marker", "documents_file": "documents.yaml"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CHAT_MODEL", "gateway/tool-model")
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.delenv("AGENT_MCP_URL", raising=False)
    monkeypatch.delenv("AGENT_BACKEND", raising=False)
    monkeypatch.delenv("PIPELINE_BACKEND", raising=False)
    monkeypatch.delenv("PIPELINE_CODEX_MODEL", raising=False)
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    monkeypatch.delenv("CODEX_BIN", raising=False)

    settings = load_settings(tmp_path)

    assert settings.agent_model == "gateway/tool-model"
    assert settings.agent_backend == "langchain"
    assert settings.pipeline_backend == "openai"
    assert settings.pipeline_codex_model == ""
    assert settings.agent_mcp_url == "http://127.0.0.1:8013/mcp"
    assert settings.agent_timeout_seconds == 120
    assert settings.agent_recursion_limit == 30
    assert settings.codex_model == ""
    assert settings.codex_bin == ""


def test_invalid_agent_backend_is_rejected(tmp_path, monkeypatch):
    _write_corpus(tmp_path, {"marker_dir": "data/marker", "documents_file": "documents.yaml"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_BACKEND", "unknown")

    with pytest.raises(CorpusConfigError, match="AGENT_BACKEND"):
        load_settings(tmp_path)


def test_pipeline_codex_model_falls_back_to_codex_model(tmp_path, monkeypatch):
    _write_corpus(tmp_path, {"marker_dir": "data/marker", "documents_file": "documents.yaml"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PIPELINE_BACKEND", "codex")
    monkeypatch.setenv("CODEX_MODEL", "account-supported-model")
    monkeypatch.delenv("PIPELINE_CODEX_MODEL", raising=False)

    settings = load_settings(tmp_path)

    assert settings.pipeline_backend == "codex"
    assert settings.pipeline_codex_model == "account-supported-model"


def test_invalid_pipeline_backend_is_rejected(tmp_path, monkeypatch):
    _write_corpus(tmp_path, {"marker_dir": "data/marker", "documents_file": "documents.yaml"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PIPELINE_BACKEND", "unknown")

    with pytest.raises(CorpusConfigError, match="PIPELINE_BACKEND"):
        load_settings(tmp_path)
