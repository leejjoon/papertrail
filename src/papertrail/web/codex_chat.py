"""ChatGPT-authenticated Codex backend for Papertrail browser research.

Codex owns its login cache and token refresh through the official SDK/App
Server. Papertrail supplies a locked-down runtime configuration, observes the
read-only MCP calls for read-before-cite validation, and persists only a final
payload that passes the same trusted validation as the LangChain backend.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from collections.abc import AsyncIterator
from typing import Any

from .. import prompts
from ..config import Settings
from ..core.citations import InvalidCitationsError, validate_and_enrich
from ..core.registry import CorpusRegistry
from ..core.store import Store
from .agent_chat import (
    READ_ONLY_TOOLS,
    TOOL_LABELS,
    AgentOutputError,
    AgentUnavailableError,
    _messages_from_history,
    validate_grounding,
)

log = logging.getLogger(__name__)

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


def codex_config_overrides(settings: Settings) -> tuple[str, ...]:
    """Build non-persistent Codex config that exposes only Papertrail research tools."""
    tools = json.dumps(sorted(READ_ONLY_TOOLS))
    url = json.dumps(settings.agent_mcp_url)
    return (
        "features.shell_tool=false",
        "features.unified_exec=false",
        'web_search="disabled"',
        "tools.view_image=false",
        "memories.generate_memories=false",
        'history.persistence="none"',
        f"mcp_servers.papertrail.url={url}",
        f"mcp_servers.papertrail.enabled_tools={tools}",
        "mcp_servers.papertrail.required=true",
        'mcp_servers.papertrail.default_tools_approval_mode="approve"',
        "mcp_servers.papertrail.startup_timeout_sec=15",
        f"mcp_servers.papertrail.tool_timeout_sec={max(1, int(settings.agent_timeout_seconds))}",
    )


def _tool_arguments(item: Any) -> dict:
    arguments = getattr(item, "arguments", {})
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _mcp_result_succeeded(item: Any) -> bool:
    """Reject MCP results whose transport succeeded but whose text reports an error."""
    result = getattr(item, "result", None)
    if result is None:
        return True
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    for block in content or []:
        text = block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")
        if isinstance(text, str) and text.lstrip().lower().startswith(
            ("error:", "unknown doc_id", "unknown node_id", "no section tree")
        ):
            return False
    return True


def _successful_mcp_reads(items: list[Any]) -> tuple[set[tuple[str, str]], list[str]]:
    """Extract successful Papertrail get_section calls and sanitized tool labels."""
    reads: set[tuple[str, str]] = set()
    labels: list[str] = []
    for wrapped in items:
        item = getattr(wrapped, "root", wrapped)
        if getattr(item, "type", None) != "mcpToolCall":
            continue
        if getattr(item, "server", None) != "papertrail":
            continue
        tool = getattr(item, "tool", None)
        if tool not in READ_ONLY_TOOLS:
            continue
        labels.append(TOOL_LABELS[tool])
        status = getattr(getattr(item, "status", None), "value", getattr(item, "status", None))
        if (tool != "get_section" or status != "completed"
                or getattr(item, "error", None) is not None
                or not _mcp_result_succeeded(item)):
            continue
        args = _tool_arguments(item)
        doc_id, node_id = args.get("doc_id"), args.get("node_id")
        if isinstance(doc_id, str) and isinstance(node_id, str):
            reads.add((doc_id, node_id))
    return reads, labels


def _format_input(messages: list[dict]) -> str:
    lines = ["Answer the latest user question using the Papertrail MCP tools."]
    if len(messages) > 1:
        lines.append("\nRecent conversation context:")
        for message in messages[:-1]:
            lines.append(f"{message['role'].upper()}: {message['content']}")
    lines.append(f"\nLATEST USER QUESTION: {messages[-1]['content']}")
    return "\n".join(lines)


class CodexAgentChatService:
    """Run a fresh, ephemeral Codex thread for each validation attempt."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.status = "not_checked"

    def health(self) -> dict:
        return {
            "backend": "codex",
            "status": self.status,
            "model": self.settings.codex_model or "Codex account default",
            "auth": "Codex login (OAuth)",
        }

    async def _run_attempt(self, messages: list[dict]) -> tuple[dict, set[tuple[str, str]], list[str]]:
        try:
            from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox
        except ImportError as exc:
            raise AgentUnavailableError(
                "Codex dependencies are not installed; install papertrail[codex]"
            ) from exc

        configured_bin = self.settings.codex_bin or shutil.which("codex")
        config = CodexConfig(
            codex_bin=configured_bin,
            cwd=tempfile.gettempdir(),
            config_overrides=codex_config_overrides(self.settings),
        )
        try:
            async with AsyncCodex(config) as codex:
                account = await codex.account()
                if account.account is None:
                    raise AgentUnavailableError(
                        "Codex is not signed in; run 'codex login' and restart Papertrail"
                    )
                self.status = "ready"
                thread = await codex.thread_start(
                    approval_mode=ApprovalMode.deny_all,
                    base_instructions=prompts.render(self.settings.corpus, "agent.system"),
                    developer_instructions=(
                        "This is a read-only Papertrail research session. Use only the "
                        "papertrail MCP server. Do not use filesystem, shell, web, plugins, "
                        "connectors, skills, or outside knowledge."
                    ),
                    cwd=tempfile.gettempdir(),
                    ephemeral=True,
                    model=self.settings.codex_model or None,
                    sandbox=Sandbox.read_only,
                )
                result = await thread.run(
                    _format_input(messages),
                    output_schema=ANSWER_SCHEMA,
                )
        except AgentUnavailableError:
            raise
        except Exception as exc:
            self.status = "unavailable"
            raise AgentUnavailableError(f"Codex could not start: {exc}") from exc

        if not result.final_response:
            raise AgentOutputError("Codex returned no final response")
        try:
            payload = json.loads(result.final_response)
        except json.JSONDecodeError as exc:
            raise AgentOutputError("Codex returned invalid structured output") from exc
        reads, labels = _successful_mcp_reads(result.items)
        return payload, reads, labels

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
                    phase = "Correcting citations" if attempt else "Researching with Codex"
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
                    payload, read_nodes, labels = await self._run_attempt(messages)
                    for label in dict.fromkeys(labels):
                        yield {"event": "step", "step": label, "status": "done"}
                    yield {"event": "step", "step": phase, "status": "done"}
                    yield {"event": "step", "step": "Validating citations", "status": "start"}
                    try:
                        validate_grounding(payload, read_nodes)
                        answer = {
                            "question": question,
                            "answer": payload["answer"],
                            "citations": payload["citations"],
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
                   "message": "The Codex research agent timed out. Please try a narrower question."}
        except AgentUnavailableError as exc:
            self.status = "unavailable"
            yield {"event": "error", "code": "agent_unavailable", "message": str(exc)}
        except AgentOutputError:
            yield {"event": "error", "code": "invalid_agent_output",
                   "message": "The Codex research agent could not produce a fully grounded answer. "
                              "Please try a narrower question."}
        except asyncio.CancelledError:
            raise
        except Exception:
            self.status = "error"
            log.exception("Codex agentic chat failed")
            yield {"event": "error", "code": "agent_failed",
                   "message": "The Codex research agent could not complete this question. "
                              "Please try again."}
