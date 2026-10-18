"""Claude Agent SDK chat backend: configuration lock-down, grounding bookkeeping,
backend selection. No test here needs the optional SDK installed."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from papertrail.core.registry import Doc
from papertrail.identity import PlainStyle
from papertrail.web.agent_chat import (
    AGENT_BACKENDS,
    AgentUnavailableError,
    AttemptState,
    _format_input,
    backend_installed,
    create_agent_chat_service,
)
from papertrail.web.claude_chat import (
    ClaudeAgentChatService,
    ResearchLimitError,
    claude_agent_options,
    claude_auth_label,
    observe_message,
)


def _settings(**overrides):
    base = {
        "agent_mcp_url": "http://127.0.0.1:8013/mcp",
        "agent_timeout_seconds": 2,
        "agent_recursion_limit": 20,
        "claude_model": "",
        "claude_bin": "",
        "agent_backend": "claude",
        "agent_model": "gateway/tool-model",
        "codex_model": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _doc():
    node = {"kind": "leaf", "block_type": "paragraph", "page": 2, "text": "jet"}
    return Doc(
        doc_id="paper", doc_name="Paper", label="Paper", has_fulltext=True,
        authors="Doe", year=2026, journal="Journal", node_by_id={"0042": node},
    )


class FakeStore:
    def __init__(self):
        self.answers = []
        self.messages = []

    def list_messages(self, conversation_id):
        return []

    def create_answer(self, kind, enriched, conversation_id=None, title=None):
        self.answers.append((kind, enriched, conversation_id))
        return "answer-1"

    def add_message(self, conversation_id, role, text=None, answer_id=None):
        self.messages.append((conversation_id, role, text, answer_id))


# --- configuration -----------------------------------------------------------

def test_claude_options_expose_only_papertrail_read_tools():
    options = claude_agent_options(_settings(), "SYSTEM")

    assert options["system_prompt"] == "SYSTEM"
    assert options["tools"] == []                      # no built-in tools at all
    assert options["permission_mode"] == "dontAsk"      # unlisted calls are denied, never prompted
    assert options["allowed_tools"] == [
        "mcp__papertrail__search_fulltext",
        "mcp__papertrail__get_section",
        "mcp__papertrail__get_figure_image",
    ]
    assert "Bash" in options["disallowed_tools"] and "WebSearch" in options["disallowed_tools"]
    assert options["mcp_servers"] == {
        "papertrail": {"type": "http", "url": "http://127.0.0.1:8013/mcp"}}
    assert options["strict_mcp_config"] is True        # ignore the user's own MCP servers
    assert options["setting_sources"] == []            # ignore ~/.claude settings/skills
    assert options["max_turns"] == 20
    assert options["output_format"]["type"] == "json_schema"
    assert options["output_format"]["schema"]["required"] == ["answer", "citations"]
    assert "model" not in options and "cli_path" not in options


def test_claude_options_pass_through_model_and_cli_overrides():
    options = claude_agent_options(_settings(claude_model="opus", claude_bin="/opt/claude"), "S")

    assert options["model"] == "opus"
    assert options["cli_path"] == "/opt/claude"


def test_claude_auth_label_names_the_credential_source_not_its_value(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert claude_auth_label() == "Claude Code login (subscription)"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    label = claude_auth_label()
    assert label.startswith("ANTHROPIC_API_KEY") and "secret" not in label


def test_claude_health_reports_safe_configuration(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    assert ClaudeAgentChatService(_settings()).health() == {
        "backend": "claude",
        "status": "not_checked",
        "model": "Claude Code default",
        "auth": "Claude Code login (subscription)",
    }


def test_agent_service_factory_selects_claude_backend():
    service = create_agent_chat_service(_settings())
    assert isinstance(service, ClaudeAgentChatService)

    explicit = create_agent_chat_service(_settings(agent_backend="codex"), "claude")
    assert isinstance(explicit, ClaudeAgentChatService)

    with pytest.raises(ValueError):
        create_agent_chat_service(_settings(agent_backend="nope"))


def test_backend_registry_covers_every_factory_choice():
    assert set(AGENT_BACKENDS) == {"langchain", "codex", "claude"}
    for name in AGENT_BACKENDS:
        assert isinstance(backend_installed(name), bool)
        assert create_agent_chat_service(_settings(agent_backend="langchain"), name).health()["backend"] == name


def test_format_input_keeps_recent_context_and_flags_latest_question():
    prompt = _format_input([
        {"role": "user", "content": "Earlier?"},
        {"role": "assistant", "content": "Earlier answer."},
        {"role": "user", "content": "Latest?"},
    ])
    assert "USER: Earlier?" in prompt and "ASSISTANT: Earlier answer." in prompt
    assert prompt.endswith("LATEST USER QUESTION: Latest?")


# --- grounding bookkeeping over the SDK stream --------------------------------

def _tool_use(call_id, name, **inputs):
    return SimpleNamespace(id=call_id, name=name, input=inputs)


def _tool_result(call_id, content, is_error=None):
    return SimpleNamespace(tool_use_id=call_id, content=content, is_error=is_error)


def test_observe_message_counts_only_successful_papertrail_section_reads():
    state = AttemptState()
    events = []
    events += observe_message(SimpleNamespace(content=[
        _tool_use("c1", "mcp__papertrail__search_fulltext", query="jet"),
        _tool_use("c2", "mcp__papertrail__get_section", doc_id="paper", node_id="0042"),
        _tool_use("c3", "mcp__papertrail__get_section", doc_id="paper", node_id="bad"),
        _tool_use("c4", "mcp__other__get_section", doc_id="paper", node_id="0099"),
        _tool_use("c5", "Read", file_path="/etc/passwd"),
    ]), state)
    events += observe_message(SimpleNamespace(content=[
        _tool_result("c1", [{"type": "text", "text": "hits"}]),
        _tool_result("c2", [{"type": "text", "text": "§ Results\n\njet detected"}]),
        _tool_result("c3", [{"type": "text", "text": "Unknown node_id 'bad'."}]),
        _tool_result("c4", "ignored"),
    ]), state)

    assert state.read_nodes == {("paper", "0042")}
    assert [e["step"] for e in events if e["status"] == "start"] == [
        "Searching the paper", "Reading a section", "Reading a section"]
    assert sum(e["status"] == "done" for e in events) == 3
    assert not any("Read" == e["step"] for e in events)


def test_observe_message_rejects_error_results_and_records_structured_output():
    state = AttemptState()
    observe_message(SimpleNamespace(content=[
        _tool_use("c1", "mcp__papertrail__get_section", doc_id="paper", node_id="0042")]), state)
    observe_message(SimpleNamespace(content=[_tool_result("c1", "whatever", is_error=True)]), state)
    assert state.read_nodes == set()

    payload = {"answer": "A [1].", "citations": [{"id": 1, "doc_id": "paper", "node_id": "0042"}]}
    observe_message(SimpleNamespace(subtype="success", is_error=False, structured_output=payload), state)
    assert state.payload == payload
    assert state.result_subtype == "success"


def test_observe_message_records_assistant_and_result_failures():
    state = AttemptState()
    observe_message(SimpleNamespace(content=[], error="authentication_failed"), state)
    assert state.failure == "authentication_failed"

    state = AttemptState()
    observe_message(SimpleNamespace(subtype="error_max_turns", is_error=True, structured_output=None), state)
    assert state.payload is None and state.failure == "error_max_turns"


# --- stream_chat end to end with a faked attempt ------------------------------

class FakeClaudeService(ClaudeAgentChatService):
    def __init__(self, settings, payload, read_nodes):
        super().__init__(settings)
        self.payload = payload
        self.read_nodes = read_nodes
        self.attempts = 0

    async def _stream_attempt(self, messages, state):
        self.attempts += 1
        state.payload = self.payload
        state.read_nodes.update(self.read_nodes)
        yield {"event": "step", "step": "Reading a section", "status": "done"}


class UnavailableClaudeService(ClaudeAgentChatService):
    async def _stream_attempt(self, messages, state):
        if False:
            yield None
        raise AgentUnavailableError("Claude Code is not logged in")


class LimitClaudeService(ClaudeAgentChatService):
    async def _stream_attempt(self, messages, state):
        if False:
            yield None
        raise ResearchLimitError("turn budget exhausted")


def _collect(service):
    store = FakeStore()
    registry = SimpleNamespace(docs={"paper": _doc()}, style=PlainStyle())

    async def run():
        return [event async for event in service.stream_chat(registry, store, 7, "Jet?")]

    return asyncio.run(run()), store


def test_claude_stream_chat_persists_only_after_strict_validation():
    payload = {"answer": "The paper reports a jet [1].",
               "citations": [{"id": 1, "doc_id": "paper", "node_id": "0042"}]}
    service = FakeClaudeService(_settings(), payload, {("paper", "0042")})

    events, store = _collect(service)

    assert events[0] == {"event": "step", "step": "Researching with Claude", "status": "start"}
    assert events[-1]["event"] == "done" and events[-1]["answer_id"] == "answer-1"
    assert store.answers[0][0] == "chat_message"
    assert store.messages == [(7, "user", "Jet?", None), (7, "assistant", None, "answer-1")]
    assert service.attempts == 1


def test_claude_stream_chat_retries_once_then_fails_without_persisting():
    payload = {"answer": "An unsupported claim [1].",
               "citations": [{"id": 1, "doc_id": "paper", "node_id": "0042"}]}
    service = FakeClaudeService(_settings(), payload, set())   # never read the node

    events, store = _collect(service)

    assert events[-1]["code"] == "invalid_agent_output"
    assert any(e.get("step") == "Correcting citations" for e in events)
    assert service.attempts == 2
    assert store.answers == [] and store.messages == []


def test_claude_unavailable_and_limit_errors_are_specific():
    events, store = _collect(UnavailableClaudeService(_settings()))
    assert events[-1]["code"] == "agent_unavailable"
    assert store.answers == []

    service = LimitClaudeService(_settings())
    events, _ = _collect(service)
    assert events[-1]["code"] == "research_limit"
