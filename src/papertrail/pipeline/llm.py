"""One place to construct the LLM gateway client.

kb/classify.py, kb/paper_pages.py, and kb/topic_pages.py each constructed
their own `OpenAI(...)` client at MODULE IMPORT time — an import-time side
effect the papertrail migration's governing rule forbids (see
papertrail.config's docstring). get_client(settings) replaces all three;
callers construct it once per run (main()), not per-call.
"""
from __future__ import annotations

from openai import OpenAI

from ..config import Settings


def get_client(settings: Settings) -> OpenAI:
    return OpenAI(base_url=settings.base_url, api_key=settings.api_key)
