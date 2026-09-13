"""Provider-neutral text generation for pipeline stages B, C, and D.

The OpenAI-compatible adapter preserves the original Chat Completions calls.
The Codex adapter owns no credentials: the official SDK/App Server reuses the
local ``codex login`` and refreshes it. Each generation uses a fresh process so
the B/C worker pools never rely on undocumented SDK client thread safety.
"""
from __future__ import annotations

import shutil
import tempfile
from typing import Any

from openai import OpenAI

from ..config import Settings


class PipelineLLMError(RuntimeError):
    """The configured pipeline provider cannot be used."""


class OpenAIPipelineClient:
    backend = "openai"

    def __init__(self, settings: Settings):
        if not settings.api_key:
            raise PipelineLLMError("API_KEY not found in .env")
        self.client = OpenAI(base_url=settings.base_url, api_key=settings.api_key)

    def model_label(self, model: str) -> str:
        return model

    def generate(
        self,
        *,
        model: str,
        system: str,
        user: str,
        output_schema: dict[str, Any],
        max_tokens: int,
        temperature: float | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        response = self.client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""


def codex_pipeline_overrides() -> tuple[str, ...]:
    """Disable general Codex capabilities for deterministic text generation."""
    return (
        "features.shell_tool=false",
        "features.unified_exec=false",
        'web_search="disabled"',
        "tools.view_image=false",
        "memories.generate_memories=false",
        'history.persistence="none"',
    )


class CodexPipelineClient:
    backend = "codex"

    def __init__(self, settings: Settings):
        self.model = settings.pipeline_codex_model
        self.codex_bin = settings.codex_bin or shutil.which("codex")
        self._check_authentication()

    def _config(self):
        try:
            from openai_codex import CodexConfig
        except ImportError as exc:
            raise PipelineLLMError(
                "Codex dependencies are not installed; install papertrail[codex]"
            ) from exc
        return CodexConfig(
            codex_bin=self.codex_bin,
            cwd=tempfile.gettempdir(),
            config_overrides=codex_pipeline_overrides(),
        )

    def _check_authentication(self) -> None:
        try:
            from openai_codex import Codex
        except ImportError as exc:
            raise PipelineLLMError(
                "Codex dependencies are not installed; install papertrail[codex]"
            ) from exc
        try:
            with Codex(self._config()) as codex:
                if codex.account().account is None:
                    raise PipelineLLMError(
                        "Codex is not signed in; run 'codex login' and retry"
                    )
        except PipelineLLMError:
            raise
        except Exception as exc:
            raise PipelineLLMError(f"Codex could not start: {exc}") from exc

    def model_label(self, _model: str) -> str:
        return self.model or "Codex account default"

    def generate(
        self,
        *,
        model: str,
        system: str,
        user: str,
        output_schema: dict[str, Any],
        max_tokens: int,
        temperature: float | None = None,
    ) -> str:
        del model, max_tokens, temperature
        try:
            from openai_codex import ApprovalMode, Codex, Sandbox
        except ImportError as exc:
            raise PipelineLLMError(
                "Codex dependencies are not installed; install papertrail[codex]"
            ) from exc
        try:
            with Codex(self._config()) as codex:
                thread = codex.thread_start(
                    approval_mode=ApprovalMode.deny_all,
                    base_instructions=system,
                    developer_instructions=(
                        "Return only the requested structured result. Do not use shell, "
                        "filesystem, web, MCP, plugins, connectors, skills, or outside knowledge."
                    ),
                    cwd=tempfile.gettempdir(),
                    ephemeral=True,
                    model=self.model or None,
                    sandbox=Sandbox.read_only,
                )
                result = thread.run(user, output_schema=output_schema)
        except Exception as exc:
            raise PipelineLLMError(f"Codex generation failed: {exc}") from exc
        if not result.final_response:
            raise PipelineLLMError("Codex returned no final response")
        return result.final_response


def get_client(settings: Settings) -> OpenAIPipelineClient | CodexPipelineClient:
    if settings.pipeline_backend == "codex":
        return CodexPipelineClient(settings)
    return OpenAIPipelineClient(settings)


def get_client_or_exit(settings: Settings):
    """CLI-friendly provider factory with one actionable terminal error."""
    try:
        return get_client(settings)
    except PipelineLLMError as exc:
        raise SystemExit(str(exc)) from exc


def worker_count(settings: Settings, requested: int | None, openai_default: int) -> int:
    if requested is not None:
        if requested < 1:
            raise SystemExit("--workers must be at least 1")
        return requested
    return 2 if settings.pipeline_backend == "codex" else openai_default
