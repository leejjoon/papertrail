import asyncio
from types import SimpleNamespace

import pytest

import papertrail.web.agent_chat as agent_chat_module
from papertrail.core.registry import Doc
from papertrail.core.store import Store
from papertrail.identity import PlainStyle
from papertrail.web.agent_chat import (
    READ_ONLY_TOOLS,
    AgentChatService,
    AgentOutputError,
    AgentUnavailableError,
    AttemptState,
    _messages_from_history,
    _tool_result_succeeded,
    create_agent_chat_service,
    validate_grounding,
)
from papertrail.web.codex_chat import (
    CodexAgentChatService,
    _successful_mcp_reads,
    codex_config_overrides,
)


def test_validate_grounding_requires_exact_markers_and_read_sections():
    payload = {
        "answer": "The jet is detected [1].",
        "citations": [{"id": 1, "doc_id": "paper", "node_id": "0042"}],
    }
    validate_grounding(payload, {("paper", "0042")})

    with pytest.raises(AgentOutputError, match="not read"):
        validate_grounding(payload, set())

    bad_markers = {**payload, "answer": "The jet is detected [2]."}
    with pytest.raises(AgentOutputError, match="markers"):
        validate_grounding(bad_markers, {("paper", "0042")})


def test_browser_agent_cannot_call_authoring_tools():
    assert READ_ONLY_TOOLS == {"search_fulltext", "get_section", "get_figure_image"}
    assert "validate_answer" not in READ_ONLY_TOOLS
    assert "publish_answer" not in READ_ONLY_TOOLS


def test_validate_grounding_allows_an_explicit_no_evidence_answer():
    validate_grounding(
        {"answer": "The corpus does not provide enough evidence to answer this.", "citations": []},
        set(),
    )


def test_mcp_text_error_content_does_not_count_as_a_successful_read():
    message = SimpleNamespace(
        status="success",
        content=[{"type": "text", "text": "Unknown node_id 'nope' in paper."}],
    )
    assert not _tool_result_succeeded(message)


def test_history_conversion_keeps_recent_user_and_answer_text():
    history = _messages_from_history([
        {"role": "user", "text": "What was observed?"},
        {"role": "assistant", "payload": {"results": [{"answer": "SiO emission [1]."}]}},
    ])
    assert history == [
        {"role": "user", "content": "What was observed?"},
        {"role": "assistant", "content": "SiO emission [1]."},
    ]


class FakeStore:
    def __init__(self):
        self.answers = []
        self.messages = []

    def list_messages(self, _conversation_id):
        return []

    def create_answer(self, kind, payload, conversation_id=None):
        self.answers.append((kind, payload, conversation_id))
        return "answer-1"

    def add_message(self, conversation_id, role, text=None, answer_id=None):
        self.messages.append((conversation_id, role, text, answer_id))


class FakeAgentService(AgentChatService):
    def __init__(self, settings, payload, read_nodes):
        super().__init__(settings)
        self.payload = payload
        self.read_nodes = read_nodes

    async def _load_tools(self):
        self.status = "ready"
        return []

    def _build_agent(self, tools):
        return object()

    async def _stream_attempt(self, agent, messages, state: AttemptState):
        state.payload = self.payload
        state.read_nodes.update(self.read_nodes)
        yield {"event": "step", "step": "Reading a section", "status": "done"}


class LimitAgentService(FakeAgentService):
    async def _stream_attempt(self, agent, messages, state: AttemptState):
        if False:
            yield None
        raise RuntimeError("simulated graph limit")


class UnavailableAgentService(AgentChatService):
    async def _load_tools(self):
        raise AgentUnavailableError("research tools unavailable")


class UnavailableCodexService(CodexAgentChatService):
    async def _run_attempt(self, messages):
        raise AgentUnavailableError("Codex is not signed in")


def _doc():
    node = {"kind": "leaf", "block_type": "paragraph", "page": 2, "text": "jet"}
    return Doc(
        doc_id="paper", doc_name="Paper", label="Paper", has_fulltext=True,
        authors="Doe", year=2026, journal="Journal", node_by_id={"0042": node},
    )


def _settings():
    return SimpleNamespace(
        agent_timeout_seconds=2,
        agent_recursion_limit=20,
        agent_model="gateway/tool-model",
    )


def test_agent_service_factory_selects_codex_backend():
    settings = SimpleNamespace(agent_backend="codex", codex_model="")

    service = create_agent_chat_service(settings)

    assert service.health() == {
        "backend": "codex",
        "status": "not_checked",
        "model": "Codex account default",
        "auth": "Codex login (OAuth)",
    }


def test_agent_health_reports_safe_langchain_configuration():
    service = AgentChatService(_settings())

    assert service.health() == {
        "backend": "langchain",
        "status": "not_checked",
        "model": "gateway/tool-model",
        "auth": "API key",
    }


@pytest.mark.parametrize("service", [
    UnavailableAgentService(_settings()),
    UnavailableCodexService(SimpleNamespace(
        agent_timeout_seconds=2,
        codex_model="",
    )),
])
def test_unavailable_agent_updates_health_status(service):
    store = FakeStore()
    registry = SimpleNamespace(docs={"paper": _doc()}, style=PlainStyle())

    async def collect():
        return [event async for event in service.stream_chat(registry, store, 7, "Jet?")]

    events = asyncio.run(collect())

    assert events[-1]["code"] == "agent_unavailable"
    assert service.health()["status"] == "unavailable"


def test_codex_configuration_disables_general_tools_and_allowlists_papertrail():
    settings = SimpleNamespace(
        agent_mcp_url="http://127.0.0.1:8013/mcp",
        agent_timeout_seconds=120,
    )

    overrides = codex_config_overrides(settings)

    assert "features.shell_tool=false" in overrides
    assert 'web_search="disabled"' in overrides
    assert "tools.view_image=false" in overrides
    assert any(value.startswith("mcp_servers.papertrail.enabled_tools=") for value in overrides)
    assert "mcp_servers.papertrail.required=true" in overrides


def test_codex_mcp_results_only_count_successful_papertrail_section_reads():
    completed = SimpleNamespace(value="completed")
    items = [
        SimpleNamespace(root=SimpleNamespace(
            type="mcpToolCall", server="papertrail", tool="get_section",
            status=completed, error=None,
            arguments={"doc_id": "paper", "node_id": "0042"},
        )),
        SimpleNamespace(root=SimpleNamespace(
            type="mcpToolCall", server="other", tool="get_section",
            status=completed, error=None,
            arguments={"doc_id": "paper", "node_id": "bad"},
        )),
    ]

    reads, labels = _successful_mcp_reads(items)

    assert reads == {("paper", "0042")}
    assert labels == ["Reading a section"]


def test_codex_mcp_text_error_does_not_count_as_a_successful_read():
    item = SimpleNamespace(root=SimpleNamespace(
        type="mcpToolCall", server="papertrail", tool="get_section",
        status=SimpleNamespace(value="completed"), error=None,
        arguments={"doc_id": "paper", "node_id": "0042"},
        result={"content": [{"type": "text", "text": "Unknown node_id '0042'."}]},
    ))

    reads, labels = _successful_mcp_reads([item])

    assert reads == set()
    assert labels == ["Reading a section"]


def test_stream_chat_persists_only_after_strict_validation():
    payload = {
        "answer": "The paper reports a jet [1].",
        "citations": [{"id": 1, "doc_id": "paper", "node_id": "0042"}],
    }
    service = FakeAgentService(_settings(), payload, {("paper", "0042")})
    store = FakeStore()
    registry = SimpleNamespace(docs={"paper": _doc()}, style=PlainStyle())

    async def collect():
        return [event async for event in service.stream_chat(registry, store, 7, "Jet?")]

    events = asyncio.run(collect())
    assert events[-1]["event"] == "done"
    assert events[-1]["answer_id"] == "answer-1"
    assert store.answers[0][0] == "chat_message"
    assert store.messages == [(7, "user", "Jet?", None), (7, "assistant", None, "answer-1")]


def test_stream_chat_retries_then_returns_error_without_persisting():
    payload = {
        "answer": "An unsupported claim [1].",
        "citations": [{"id": 1, "doc_id": "paper", "node_id": "0042"}],
    }
    service = FakeAgentService(_settings(), payload, set())
    store = FakeStore()
    registry = SimpleNamespace(docs={"paper": _doc()}, style=PlainStyle())

    async def collect():
        return [event async for event in service.stream_chat(registry, store, 7, "Jet?")]

    events = asyncio.run(collect())
    assert events[-1] == {
        "event": "error",
        "code": "invalid_agent_output",
        "message": "The research agent could not produce a fully grounded answer. "
                   "Please try a narrower question.",
    }
    assert any(event.get("step") == "Correcting citations" for event in events)
    assert store.answers == []
    assert store.messages == []


def test_recursion_limit_has_a_specific_terminal_error(monkeypatch):
    monkeypatch.setattr(agent_chat_module, "_is_graph_recursion_error", lambda exc: True)
    service = LimitAgentService(_settings(), {}, set())
    store = FakeStore()
    registry = SimpleNamespace(docs={"paper": _doc()}, style=PlainStyle())

    async def collect():
        return [event async for event in service.stream_chat(registry, store, 7, "IGRINS?")]

    events = asyncio.run(collect())
    assert events[-1] == {
        "event": "error",
        "code": "research_limit",
        "message": "The research agent exhausted its tool budget. Please ask a narrower question.",
    }


def test_deleting_conversation_removes_its_chat_answers(tmp_path):
    store = Store(SimpleNamespace(sqlite_path=tmp_path / "kb.sqlite"))
    conversation_id = store.create_conversation("Temporary")
    answer_id = store.create_answer(
        "chat_message", {"results": []}, conversation_id=conversation_id
    )
    store.add_message(conversation_id, "assistant", answer_id=answer_id)

    store.delete_conversation(conversation_id)

    assert store.get_answer(answer_id) is None
