import json
from types import SimpleNamespace

import pytest

import papertrail.pipeline.llm as llm_module
from papertrail.identity import PlainStyle
from papertrail.pipeline import classify, paper_pages, topic_pages
from papertrail.pipeline.llm import (
    CodexPipelineClient,
    OpenAIPipelineClient,
    PipelineLLMError,
    codex_pipeline_overrides,
    get_client,
    worker_count,
)


class FakePipelineClient:
    backend = "fake"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.responses)

    def model_label(self, model):
        return model


def test_openai_pipeline_requires_api_key():
    with pytest.raises(PipelineLLMError, match="API_KEY"):
        OpenAIPipelineClient(SimpleNamespace(api_key="", base_url=""))


def test_openai_pipeline_preserves_chat_completions_request(monkeypatch):
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            message = SimpleNamespace(content="gateway output")
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(llm_module, "OpenAI", FakeOpenAI)
    client = OpenAIPipelineClient(SimpleNamespace(api_key="secret", base_url="https://gateway"))

    result = client.generate(
        model="gateway-model", system="system", user="user",
        output_schema={"type": "object"}, max_tokens=1234, temperature=0.2,
    )

    assert result == "gateway output"
    assert captured["client"] == {"base_url": "https://gateway", "api_key": "secret"}
    assert captured["model"] == "gateway-model"
    assert captured["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
    ]
    assert captured["max_tokens"] == 1234
    assert captured["temperature"] == 0.2


def test_pipeline_factory_selects_codex_and_resolves_model(monkeypatch):
    monkeypatch.setattr(CodexPipelineClient, "_check_authentication", lambda self: None)
    settings = SimpleNamespace(
        pipeline_backend="codex",
        pipeline_codex_model="pipeline-model",
        codex_bin="/opt/codex",
    )

    client = get_client(settings)

    assert isinstance(client, CodexPipelineClient)
    assert client.model_label("ignored") == "pipeline-model"


def test_codex_pipeline_is_ephemeral_read_only_and_structured(monkeypatch):
    openai_codex = pytest.importorskip("openai_codex")
    captured = {}

    class FakeAccount:
        account = object()

    class FakeThread:
        def run(self, user, output_schema):
            captured["user"] = user
            captured["schema"] = output_schema
            return SimpleNamespace(final_response='{"result":"ok"}')

    class FakeCodex:
        def __init__(self, config):
            captured["config"] = config

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def account(self):
            return FakeAccount()

        def thread_start(self, **kwargs):
            captured["thread"] = kwargs
            return FakeThread()

    monkeypatch.setattr(openai_codex, "Codex", FakeCodex)
    settings = SimpleNamespace(
        pipeline_codex_model="pipeline-model",
        codex_bin="/opt/codex",
    )
    client = CodexPipelineClient(settings)
    schema = {"type": "object"}

    result = client.generate(
        model="ignored", system="system", user="input", output_schema=schema,
        max_tokens=1000, temperature=0.1,
    )

    assert result == '{"result":"ok"}'
    assert captured["schema"] == schema
    assert captured["thread"]["ephemeral"] is True
    assert captured["thread"]["model"] == "pipeline-model"
    assert captured["thread"]["sandbox"].value == "read-only"
    assert "features.shell_tool=false" in codex_pipeline_overrides()
    assert 'web_search="disabled"' in codex_pipeline_overrides()


def test_backend_aware_worker_defaults():
    assert worker_count(SimpleNamespace(pipeline_backend="codex"), None, 8) == 2
    assert worker_count(SimpleNamespace(pipeline_backend="openai"), None, 8) == 8
    assert worker_count(SimpleNamespace(pipeline_backend="codex"), 4, 8) == 4
    with pytest.raises(SystemExit, match="at least 1"):
        worker_count(SimpleNamespace(pipeline_backend="codex"), 0, 8)


def test_generated_page_metadata_records_provider_and_model():
    metadata = paper_pages.front_matter(
        {"doc_id": "paper", "title": "Paper"}, {}, [], "synthetic",
        "Codex account default", {}, "prompt-hash", "codex",
    )

    assert "backend: codex" in metadata
    assert "model: Codex account default" in metadata


def test_classification_retries_with_validation_feedback():
    taxonomy = {
        "method": {"multi": False, "values": ["spectroscopy", "photometry"]},
    }
    client = FakePipelineClient([
        '{"method":"invalid","confidence":"low","evidence":"none"}',
        '{"method":"spectroscopy","confidence":"high","evidence":"spectrum"}',
    ])

    result = classify.classify_one(client, "model", "system", "Paper", "body", taxonomy)

    assert result["method"] == "spectroscopy"
    assert "failed validation" in client.calls[1]["user"]
    assert client.calls[0]["output_schema"]["properties"]["method"]["enum"] == [
        "spectroscopy", "photometry",
    ]


def test_paper_page_generation_uses_structured_schema_and_validator():
    client = FakePipelineClient([
        json.dumps({"body_markdown": "## Overview\nBad [1]", "citations": [
            {"id": 1, "node_id": "bad"},
        ]}),
        json.dumps({"body_markdown": "## Overview\nGood [1]", "citations": [
            {"id": 1, "node_id": "0001"},
        ]}),
    ])

    result, _signals = paper_pages.generate_page(
        client, "model", "paper", "system", "Paper", "metadata", ["Overview"],
        "pool", {"0001"}, PlainStyle(),
    )

    assert result["citations"][0]["node_id"] == "0001"
    assert "failed validation" in client.calls[1]["user"]
    assert client.calls[0]["output_schema"] == paper_pages.PAGE_OUTPUT_SCHEMA


def test_topic_generation_uses_structured_schema_and_validator(monkeypatch):
    monkeypatch.setattr(topic_pages, "paper_digest", lambda *_args: ("Digest [1]", {"0001"}))
    monkeypatch.setattr(topic_pages, "node_text_for", lambda *_args: "Evidence")
    monkeypatch.setattr(topic_pages, "build_topic_prompt", lambda *_args: ("system", "user"))
    client = FakePipelineClient([
        json.dumps({"prose": "Bad", "citations": []}),
        json.dumps({"prose": "Good [1]", "citations": [
            {"id": 1, "doc_id": "paper", "node_id": "0001"},
        ]}),
    ])
    settings = SimpleNamespace(synth_model="model")

    result = topic_pages.synthesize_topic(
        settings, client, "method", "spectroscopy",
        [{"doc_id": "paper", "title": "Paper", "year": 2026}], ["Overview"],
    )

    assert result["prose"] == "Good [1]"
    assert "failed validation" in client.calls[1]["user"]
    assert client.calls[0]["output_schema"] == topic_pages.TOPIC_OUTPUT_SCHEMA
