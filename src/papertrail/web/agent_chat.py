"""Agentic browser chat over Papertrail's read-only MCP surface.

The model discovers and reads evidence through the same MCP server external
agents use.  This module deliberately keeps persistence and citation validation
outside the agent: MCP access is read-only, while trusted Papertrail code checks
the final payload and writes it to the existing conversation store.
"""
from __future__ import annotations

import asyncio
import logging
import re
import warnings
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .. import prompts
from ..config import Settings
from ..core.citations import InvalidCitationsError, validate_and_enrich
from ..core.registry import CorpusRegistry
from ..core.store import Store

log = logging.getLogger(__name__)

READ_ONLY_TOOL_NAMES = (
    "search_fulltext",
    "get_section",
    "get_figure_image",
)
READ_ONLY_TOOLS = frozenset(READ_ONLY_TOOL_NAMES)
REQUIRED_TOOLS = frozenset({"search_fulltext", "get_section"})

TOOL_LABELS = {
    "search_fulltext": "Searching the paper",
    "get_section": "Reading a section",
    "get_figure_image": "Inspecting a figure",
}

_MARKER_RE = re.compile(r"\[(\d+)\]")

# The structured answer every backend must return (draft-07 JSON Schema, so the
# Codex and Claude SDK validators both accept it).
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "minimum": 1},
                    "doc_id": {"type": "string"},
                    "node_id": {"type": "string"},
                },
                "required": ["id", "doc_id", "node_id"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["answer", "citations"],
    "additionalProperties": False,
}

# Selectable chat backends: id -> (UI label, importable packages its extra provides).
AGENT_BACKENDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "langchain": ("LangGraph", ("langchain", "langchain_openai")),
    "codex": ("Codex", ("openai_codex",)),
    "claude": ("Claude", ("claude_agent_sdk",)),
}


class AgentUnavailableError(RuntimeError):
    """The optional runtime or configured MCP service is unavailable."""


class AgentOutputError(ValueError):
    """The agent returned an answer that violates the grounding contract."""


def _is_graph_recursion_error(exc: Exception) -> bool:
    """Recognize LangGraph's optional limit exception without importing it at startup."""
    try:
        from langgraph.errors import GraphRecursionError
    except ImportError:
        return False
    return isinstance(exc, GraphRecursionError)


@dataclass
class AttemptState:
    payload: dict | None = None
    read_nodes: set[tuple[str, str]] = field(default_factory=set)
    pending_calls: dict[str, tuple[str, dict]] = field(default_factory=dict)
    announced_calls: set[str] = field(default_factory=set)
    # Used by backends whose stream reports terminal/auth failures as data.
    failure: str | None = None
    result_subtype: str | None = None


def _as_dict(value: Any) -> dict | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return None


def _messages_from_history(messages: list[dict]) -> list[dict]:
    """Convert Papertrail's compact stored messages to model chat messages."""
    out = []
    for message in messages[-12:]:
        if message["role"] == "user" and message.get("text"):
            out.append({"role": "user", "content": message["text"]})
        elif message["role"] == "assistant" and message.get("payload"):
            answers = [r.get("answer", "") for r in message["payload"].get("results", [])]
            content = "\n\n".join(answer for answer in answers if answer)
            if content:
                out.append({"role": "assistant", "content": content})
    return out


def _format_input(messages: list[dict]) -> str:
    """Flatten model chat messages into one prompt for single-prompt SDK sessions."""
    lines = ["Answer the latest user question using the Papertrail MCP tools."]
    if len(messages) > 1:
        lines.append("\nRecent conversation context:")
        for message in messages[:-1]:
            lines.append(f"{message['role'].upper()}: {message['content']}")
    lines.append(f"\nLATEST USER QUESTION: {messages[-1]['content']}")
    return "\n".join(lines)


def validate_grounding(payload: dict, read_nodes: set[tuple[str, str]]) -> None:
    """Enforce marker shape and the stronger read-before-cite invariant."""
    answer = payload.get("answer")
    citations = payload.get("citations")
    if not isinstance(answer, str) or not answer.strip():
        raise AgentOutputError("the answer is empty")
    if not isinstance(citations, list):
        raise AgentOutputError("citations must be a list")

    ids = [citation.get("id") for citation in citations if isinstance(citation, dict)]
    if len(ids) != len(citations) or any(not isinstance(cid, int) or cid < 1 for cid in ids):
        raise AgentOutputError("every citation needs a positive integer id")
    if len(set(ids)) != len(ids):
        raise AgentOutputError("citation ids must be unique")
    if ids and sorted(ids) != list(range(1, len(ids) + 1)):
        raise AgentOutputError("citation ids must be consecutive starting at 1")

    markers = {int(value) for value in _MARKER_RE.findall(answer)}
    if markers != set(ids):
        raise AgentOutputError("inline markers must exactly match citation ids")

    unread = []
    for citation in citations:
        key = (citation.get("doc_id"), citation.get("node_id"))
        if key not in read_nodes:
            unread.append({"doc_id": key[0], "node_id": key[1]})
    if unread:
        raise AgentOutputError(f"citations were not read with get_section: {unread}")


def _tool_result_succeeded(message: Any) -> bool:
    if getattr(message, "status", None) == "error":
        return False
    content = getattr(message, "content", "")
    if isinstance(content, list):
        content = "\n".join(
            item.get("text", "") for item in content if isinstance(item, dict)
        )
    if isinstance(content, str):
        return not content.startswith(("Unknown node_id", "No section tree", "Error:"))
    return True


class AgentChatService:
    """Lazy agent facade shared by all chat requests in one FastAPI app."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.status = "not_checked"
        self._tools: list[Any] | None = None
        self._tool_lock = asyncio.Lock()

    def health(self) -> dict:
        return {
            "backend": "langchain",
            "status": self.status,
            "model": self.settings.agent_model,
            "auth": "API key",
        }

    async def _load_tools(self) -> list[Any]:
        if self._tools is not None:
            return self._tools
        async with self._tool_lock:
            if self._tools is not None:
                return self._tools
            try:
                from langchain_core._api import LangChainBetaWarning
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", LangChainBetaWarning)
                    from langchain.mcp import MCPAdapter
            except ImportError as exc:
                self.status = "unavailable"
                raise AgentUnavailableError(
                    "agent dependencies are not installed; install papertrail[agent]") from exc

            try:
                adapter = MCPAdapter(self.settings.agent_mcp_url)
                discovered = await asyncio.wait_for(adapter.list_tools(cache_mode="refresh"), timeout=15)
            except Exception as exc:
                self.status = "unavailable"
                raise AgentUnavailableError("could not connect to the Papertrail MCP server") from exc

            by_name = {tool.name: tool for tool in discovered}
            missing = sorted(REQUIRED_TOOLS - by_name.keys())
            if missing:
                self.status = "unavailable"
                raise AgentUnavailableError(f"MCP server is missing required tools: {', '.join(missing)}")
            self._tools = [by_name[name] for name in READ_ONLY_TOOL_NAMES if name in by_name]
            self.status = "ready"
            return self._tools

    def _build_agent(self, tools: list[Any]):
        if not self.settings.api_key:
            raise AgentUnavailableError("API_KEY is missing; configure the model gateway first")
        try:
            from langchain.agents import create_agent
            from langchain.agents.structured_output import ToolStrategy
            from langchain_openai import ChatOpenAI
            from pydantic import BaseModel, Field
        except ImportError as exc:
            raise AgentUnavailableError(
                "agent dependencies are not installed; install papertrail[agent]") from exc

        class Citation(BaseModel):
            id: int = Field(ge=1)
            doc_id: str
            node_id: str

        class Answer(BaseModel):
            answer: str
            citations: list[Citation]

        model_kwargs: dict[str, Any] = {
            "model": self.settings.agent_model,
            "api_key": self.settings.api_key or None,
            "timeout": self.settings.agent_timeout_seconds,
            "max_tokens": 2500,
        }
        if self.settings.base_url:
            model_kwargs["base_url"] = self.settings.base_url
        model = ChatOpenAI(**model_kwargs)
        return create_agent(
            model,
            tools,
            system_prompt=prompts.render(self.settings.corpus, "agent.system"),
            response_format=ToolStrategy(Answer),
            name="papertrail-research-agent",
        )

    async def _stream_attempt(
        self, agent: Any, messages: list[dict], state: AttemptState
    ) -> AsyncIterator[dict]:
        """Run one agent attempt and translate graph updates into safe UI steps."""
        config = {"recursion_limit": self.settings.agent_recursion_limit}
        async for update in agent.astream({"messages": messages}, config=config, stream_mode="updates"):
            if not isinstance(update, dict):
                continue
            for node_update in update.values():
                if not isinstance(node_update, dict):
                    continue
                structured = _as_dict(node_update.get("structured_response"))
                if structured is not None:
                    state.payload = structured

                for message in node_update.get("messages", []):
                    for call in getattr(message, "tool_calls", []) or []:
                        call_id = call.get("id")
                        name = call.get("name")
                        args = call.get("args") or {}
                        if call_id and name in READ_ONLY_TOOLS:
                            state.pending_calls[call_id] = (name, args)
                            if call_id not in state.announced_calls:
                                state.announced_calls.add(call_id)
                                yield {"event": "step", "step": TOOL_LABELS[name], "status": "start"}

                    call_id = getattr(message, "tool_call_id", None)
                    if call_id and call_id in state.pending_calls:
                        name, args = state.pending_calls[call_id]
                        if name == "get_section" and _tool_result_succeeded(message):
                            doc_id, node_id = args.get("doc_id"), args.get("node_id")
                            if isinstance(doc_id, str) and isinstance(node_id, str):
                                state.read_nodes.add((doc_id, node_id))
                        yield {"event": "step", "step": TOOL_LABELS[name], "status": "done"}

    async def stream_chat(
        self,
        registry: CorpusRegistry,
        store: Store,
        conversation_id: int,
        question: str,
    ) -> AsyncIterator[dict]:
        """Run the bounded agent, validate its citations, persist, and stream SSE data."""
        try:
            yield {"event": "step", "step": "Connecting to the research tools", "status": "start"}
            tools = await self._load_tools()
            agent = self._build_agent(tools)
            self.status = "ready"
            yield {"event": "step", "step": "Connecting to the research tools", "status": "done"}

            history = _messages_from_history(store.list_messages(conversation_id))
            base_messages = [*history, {"role": "user", "content": question}]
            enriched = None
            last_error = ""

            async with asyncio.timeout(self.settings.agent_timeout_seconds):
                for attempt in range(2):
                    state = AttemptState()
                    messages = list(base_messages)
                    if attempt:
                        messages.append({
                            "role": "user",
                            "content": (
                                "Your previous output failed Papertrail's grounding checks: "
                                f"{last_error}. Research again as needed and return a corrected answer."
                            ),
                        })
                        yield {"event": "step", "step": "Correcting citations", "status": "start"}
                    else:
                        yield {"event": "step", "step": "Planning the answer", "status": "start"}

                    async for event in self._stream_attempt(agent, messages, state):
                        yield event

                    phase = "Correcting citations" if attempt else "Planning the answer"
                    yield {"event": "step", "step": phase, "status": "done"}
                    yield {"event": "step", "step": "Validating citations", "status": "start"}
                    try:
                        if state.payload is None:
                            raise AgentOutputError("the model did not return a structured answer")
                        validate_grounding(state.payload, state.read_nodes)
                        payload = {
                            "question": question,
                            "answer": state.payload["answer"],
                            "citations": state.payload["citations"],
                        }
                        enriched = validate_and_enrich(payload, registry.docs, registry.style, strict=True)
                    except (AgentOutputError, InvalidCitationsError, ValueError) as exc:
                        last_error = str(exc)
                        yield {"event": "step", "step": "Validating citations", "status": "done",
                               "ok": False}
                        if attempt == 0:
                            continue
                        raise AgentOutputError(last_error) from exc

                    yield {"event": "step", "step": "Validating citations", "status": "done",
                           "ok": True}
                    break

            if enriched is None:
                raise AgentOutputError(last_error or "the answer could not be validated")

            answer_id = store.create_answer("chat_message", enriched, conversation_id=conversation_id)
            store.add_message(conversation_id, "user", text=question)
            store.add_message(conversation_id, "assistant", answer_id=answer_id)
            yield {"event": "done", "answer_id": answer_id, **enriched}
        except TimeoutError:
            yield {"event": "error", "code": "agent_timeout",
                   "message": "The research agent timed out. Please try a narrower question."}
        except AgentUnavailableError as exc:
            self.status = "unavailable"
            yield {"event": "error", "code": "agent_unavailable", "message": str(exc)}
        except AgentOutputError:
            yield {"event": "error", "code": "invalid_agent_output",
                   "message": "The research agent could not produce a fully grounded answer. "
                              "Please try a narrower question."}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _is_graph_recursion_error(exc):
                yield {"event": "error", "code": "research_limit",
                       "message": "The research agent exhausted its tool budget. "
                                  "Please ask a narrower question."}
                return
            self.status = "error"
            self._tools = None
            log.exception("agentic chat failed")
            yield {"event": "error", "code": "agent_failed",
                   "message": "The research agent could not complete this question. Please try again."}


def backend_installed(backend: str) -> bool:
    """Whether the optional packages behind a backend are importable (no import side effects)."""
    import importlib.util

    _, packages = AGENT_BACKENDS[backend]
    return all(importlib.util.find_spec(package) is not None for package in packages)


def create_agent_chat_service(settings: Settings, backend: str | None = None):
    """Build one research backend without importing optional SDKs eagerly.

    ``backend`` defaults to ``settings.agent_backend``; the web app builds one
    service per entry in ``AGENT_BACKENDS`` so a browser user can pick per request.
    """
    backend = backend or settings.agent_backend
    if backend not in AGENT_BACKENDS:
        raise ValueError(f"unknown agent backend: {backend!r}")
    if backend == "codex":
        from .codex_chat import CodexAgentChatService

        return CodexAgentChatService(settings)
    if backend == "claude":
        from .claude_chat import ClaudeAgentChatService

        return ClaudeAgentChatService(settings)
    return AgentChatService(settings)
