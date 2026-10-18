"""Claude Agent SDK backend for Papertrail browser research.

The SDK runs the Claude Code CLI (its bundled copy, or ``CLAUDE_BIN``) as a
subprocess and inherits whatever login that CLI has: a Claude subscription
signed in with ``claude auth login``, or ``ANTHROPIC_API_KEY`` when the server's
environment sets one.  Papertrail never reads or stores those credentials.

Each validation attempt is a fresh, isolated session: no built-in tools, no
user/project settings, only the read-only Papertrail MCP tools.  Papertrail
observes ``get_section`` calls for read-before-cite validation and persists only
a payload that passes the same trusted checks as the other backends.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections.abc import AsyncIterator
from typing import Any

from .. import prompts
from ..config import Settings
from ..core.citations import InvalidCitationsError, validate_and_enrich
from ..core.registry import CorpusRegistry
from ..core.store import Store
from .agent_chat import (
    ANSWER_SCHEMA,
    READ_ONLY_TOOL_NAMES,
    READ_ONLY_TOOLS,
    TOOL_LABELS,
    AgentOutputError,
    AgentUnavailableError,
    AttemptState,
    _format_input,
    _messages_from_history,
    validate_grounding,
)

log = logging.getLogger(__name__)

MCP_SERVER_NAME = "papertrail"
MCP_TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"

# Belt and braces: ``tools=[]`` already removes every built-in tool, and these
# bare-name deny rules guarantee it even if a future CLI default changes.
DENIED_BUILTIN_TOOLS = (
    "Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep",
    "WebSearch", "WebFetch", "Agent", "Task", "Skill", "TodoWrite",
)

_ERROR_TEXT_PREFIXES = ("error:", "unknown doc_id", "unknown node_id", "no section tree")


class ResearchLimitError(AgentOutputError):
    """The session hit its turn budget before producing an answer."""


def claude_agent_options(settings: Settings, system_prompt: str) -> dict[str, Any]:
    """Keyword arguments for ``ClaudeAgentOptions`` — a locked-down research session.

    Returned as a plain dict so the configuration can be unit-tested without the
    optional SDK installed.
    """
    options: dict[str, Any] = {
        "system_prompt": system_prompt,
        "tools": [],
        "allowed_tools": [f"{MCP_TOOL_PREFIX}{name}" for name in READ_ONLY_TOOL_NAMES],
        "disallowed_tools": list(DENIED_BUILTIN_TOOLS),
        "permission_mode": "dontAsk",
        "mcp_servers": {MCP_SERVER_NAME: {"type": "http", "url": settings.agent_mcp_url}},
        "strict_mcp_config": True,
        "setting_sources": [],
        "cwd": tempfile.gettempdir(),
        "max_turns": int(settings.agent_recursion_limit),
        "output_format": {"type": "json_schema", "schema": ANSWER_SCHEMA},
    }
    if getattr(settings, "claude_model", ""):
        options["model"] = settings.claude_model
    if getattr(settings, "claude_bin", ""):
        options["cli_path"] = settings.claude_bin
    return options


def claude_auth_label() -> str:
    """Describe which credential the Claude Code subprocess will use (never its value)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY (API billing)"
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "CLAUDE_CODE_OAUTH_TOKEN"
    return "Claude Code login (subscription)"


def _papertrail_tool(name: Any) -> str | None:
    if isinstance(name, str) and name.startswith(MCP_TOOL_PREFIX):
        tool = name[len(MCP_TOOL_PREFIX):]
        if tool in READ_ONLY_TOOLS:
            return tool
    return None


def _result_block_succeeded(block: Any) -> bool:
    """Reject tool results whose transport succeeded but whose text reports an error."""
    if getattr(block, "is_error", None):
        return False
    content = getattr(block, "content", None)
    if isinstance(content, list):
        content = "\n".join(
            item.get("text", "") for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    if isinstance(content, str):
        return not content.lstrip().lower().startswith(_ERROR_TEXT_PREFIXES)
    return True


def observe_message(message: Any, state: AttemptState) -> list[dict]:
    """Fold one SDK stream message into the attempt state; return UI step events.

    Duck-typed on the SDK dataclasses (``AssistantMessage``/``UserMessage`` carry
    ``content`` blocks; ``ResultMessage`` carries ``subtype``/``structured_output``)
    so the grounding bookkeeping can be tested without the SDK.
    """
    events: list[dict] = []

    error = getattr(message, "error", None)
    if isinstance(error, str) and error:
        state.failure = error

    content = getattr(message, "content", None)
    if isinstance(content, list):
        for block in content:
            tool = _papertrail_tool(getattr(block, "name", None))
            call_id = getattr(block, "id", None)
            if tool and isinstance(call_id, str) and hasattr(block, "input"):
                args = block.input if isinstance(block.input, dict) else {}
                state.pending_calls[call_id] = (tool, args)
                if call_id not in state.announced_calls:
                    state.announced_calls.add(call_id)
                    events.append({"event": "step", "step": TOOL_LABELS[tool], "status": "start"})
                continue

            result_id = getattr(block, "tool_use_id", None)
            if isinstance(result_id, str) and result_id in state.pending_calls:
                tool, args = state.pending_calls.pop(result_id)
                if tool == "get_section" and _result_block_succeeded(block):
                    doc_id, node_id = args.get("doc_id"), args.get("node_id")
                    if isinstance(doc_id, str) and isinstance(node_id, str):
                        state.read_nodes.add((doc_id, node_id))
                events.append({"event": "step", "step": TOOL_LABELS[tool], "status": "done"})

    subtype = getattr(message, "subtype", None)
    if hasattr(message, "structured_output") and isinstance(subtype, str):
        state.result_subtype = subtype
        if subtype == "success" and isinstance(message.structured_output, dict):
            state.payload = message.structured_output
        elif getattr(message, "is_error", False):
            state.failure = subtype
    return events


class ClaudeAgentChatService:
    """Run a fresh, isolated Claude Agent SDK session for each validation attempt."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.status = "not_checked"

    def health(self) -> dict:
        model = getattr(self.settings, "claude_model", "") or "Claude Code default"
        return {
            "backend": "claude",
            "status": self.status,
            "model": model,
            "auth": claude_auth_label(),
        }

    def _sdk(self):
        try:
            import claude_agent_sdk
        except ImportError as exc:
            self.status = "unavailable"
            raise AgentUnavailableError(
                "Claude Agent SDK is not installed; install papertrail[claude]"
            ) from exc
        return claude_agent_sdk

    async def _stream_attempt(self, messages: list[dict], state: AttemptState) -> AsyncIterator[dict]:
        sdk = self._sdk()
        options = sdk.ClaudeAgentOptions(**claude_agent_options(
            self.settings, prompts.render(self.settings.corpus, "agent.system")))
        try:
            async for message in sdk.query(prompt=_format_input(messages), options=options):
                for event in observe_message(message, state):
                    yield event
                if state.failure == "authentication_failed":
                    raise AgentUnavailableError(
                        "Claude Code is not logged in; run 'claude auth login' and restart Papertrail")
                if state.failure == "billing_error":
                    raise AgentUnavailableError(
                        "Claude reported a billing problem for the signed-in account")
        except sdk.CLINotFoundError as exc:
            raise AgentUnavailableError(
                "Claude Code CLI was not found; install it or set CLAUDE_BIN") from exc
        except sdk.CLIConnectionError as exc:
            raise AgentUnavailableError(f"Claude Code could not start: {exc}") from exc
        except sdk.ProcessError as exc:
            # ResultError subclasses ProcessError and carries the CLI's error result.
            subtype = getattr(exc, "subtype", None) or state.result_subtype
            if subtype == "error_max_turns" or getattr(exc, "terminal_reason", None) == "max_turns":
                raise ResearchLimitError("the research agent exhausted its turn budget") from exc
            if getattr(exc, "api_error_status", None) in (401, 403):
                raise AgentUnavailableError(
                    "Claude Code is not logged in; run 'claude auth login' and restart Papertrail"
                ) from exc
            raise AgentOutputError(f"Claude Code ended with {subtype or 'an error'}") from exc
        self.status = "ready"

    async def stream_chat(
        self,
        registry: CorpusRegistry,
        store: Store,
        conversation_id: int,
        question: str,
    ) -> AsyncIterator[dict]:
        try:
            history = _messages_from_history(store.list_messages(conversation_id))
            base_messages = [*history, {"role": "user", "content": question}]
            enriched = None
            last_error = ""

            async with asyncio.timeout(self.settings.agent_timeout_seconds):
                for attempt in range(2):
                    state = AttemptState()
                    phase = "Correcting citations" if attempt else "Researching with Claude"
                    yield {"event": "step", "step": phase, "status": "start"}
                    messages = list(base_messages)
                    if attempt:
                        messages.append({
                            "role": "user",
                            "content": (
                                "The previous result failed Papertrail's grounding checks: "
                                f"{last_error}. Research again and correct it."
                            ),
                        })
                    async for event in self._stream_attempt(messages, state):
                        yield event
                    yield {"event": "step", "step": phase, "status": "done"}
                    yield {"event": "step", "step": "Validating citations", "status": "start"}
                    try:
                        if state.payload is None:
                            raise AgentOutputError("the model did not return a structured answer")
                        validate_grounding(state.payload, state.read_nodes)
                        answer = {
                            "question": question,
                            "answer": state.payload["answer"],
                            "citations": state.payload["citations"],
                        }
                        enriched = validate_and_enrich(
                            answer, registry.docs, registry.style, strict=True
                        )
                    except (AgentOutputError, InvalidCitationsError, ValueError) as exc:
                        last_error = str(exc)
                        yield {"event": "step", "step": "Validating citations",
                               "status": "done", "ok": False}
                        if attempt == 0:
                            continue
                        raise AgentOutputError(last_error) from exc
                    yield {"event": "step", "step": "Validating citations",
                           "status": "done", "ok": True}
                    break

            if enriched is None:
                raise AgentOutputError(last_error or "the answer could not be validated")
            answer_id = store.create_answer(
                "chat_message", enriched, conversation_id=conversation_id
            )
            store.add_message(conversation_id, "user", text=question)
            store.add_message(conversation_id, "assistant", answer_id=answer_id)
            yield {"event": "done", "answer_id": answer_id, **enriched}
        except TimeoutError:
            yield {"event": "error", "code": "agent_timeout",
                   "message": "The Claude research agent timed out. Please try a narrower question."}
        except AgentUnavailableError as exc:
            self.status = "unavailable"
            yield {"event": "error", "code": "agent_unavailable", "message": str(exc)}
        except ResearchLimitError:
            yield {"event": "error", "code": "research_limit",
                   "message": "The Claude research agent exhausted its tool budget. "
                              "Please ask a narrower question."}
        except AgentOutputError:
            yield {"event": "error", "code": "invalid_agent_output",
                   "message": "The Claude research agent could not produce a fully grounded answer. "
                              "Please try a narrower question."}
        except asyncio.CancelledError:
            raise
        except Exception:
            self.status = "error"
            log.exception("Claude agentic chat failed")
            yield {"event": "error", "code": "agent_failed",
                   "message": "The Claude research agent could not complete this question. "
                              "Please try again."}
