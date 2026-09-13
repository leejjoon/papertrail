"""Prompt templates for the LLM-authored pipeline stages (classify, paper
pages, topic pages, chat) — corpus.yaml's `subject`/`citation_example`
interpolated into package-default templates (papertrail migration Step 9,
Seam 2, landed last since it's the only seam that invalidates generated
artifacts).

Format: string.Template with strict .substitute() (not .safe_substitute —
a typo'd $var should raise loudly at generation time, not silently emit a
literal "$var" into a paid LLM call), not str.format: every one of these
prompts embeds literal JSON/YAML example blocks full of `{`/`}` braces, and
doubling every one in a file corpus authors are meant to hand-edit is a
booby trap. Not Jinja2: not installed, and a whole templating language is
overkill for a handful of substitutions.

Only SYSTEM prompts (the corpus-voice, subject-dependent part) are
templated here — the USER content (title/body/node-pool assembly) is pure
data formatting, not corpus voice, and stays as ordinary Python string
building in each pipeline stage, so overriding a system template can't
accidentally break the node-pool contract a stage depends on.
"""
from __future__ import annotations

import hashlib
import re
from importlib import resources
from string import Template

PROMPT_SCHEMA_VERSION = "2"

_KNOWN_TEMPLATES = {
    "classify.system", "paper_page.system", "topic_page.system",
    "index_intro.system", "agent.system",
}


def _load_text(corpus, name: str) -> str:
    """`prompts_dir/<name>.txt` replaces a package default WHOLESALE — no
    inheritance, no partials; a corpus that overrides a template owns it."""
    prompts_dir = corpus.raw.get("prompts_dir")
    if prompts_dir:
        override = corpus.root / prompts_dir / f"{name}.txt"
        if override.exists():
            return override.read_text(encoding="utf-8")
    return resources.files("papertrail").joinpath("prompts", f"{name}.txt").read_text(encoding="utf-8")


def base_vars(corpus) -> dict:
    """Always-available template variables, corpus.yaml's `corpus:` block."""
    c = corpus.raw.get("corpus") or {}
    example = c.get("citation_example") or {}
    return {
        "subject": c.get("subject") or "the documents in this corpus",
        "citation_example_bad": example.get("bad", ""),
        "citation_example_good": example.get("good", ""),
    }


def render(corpus, name: str, **extra) -> str:
    if name not in _KNOWN_TEMPLATES:
        raise ValueError(f"unknown prompt template: {name!r}")
    text = _load_text(corpus, name)
    return Template(text).substitute({**base_vars(corpus), **extra})


def stage_prompt_version(rendered_system: str) -> str:
    """Hashes the RENDERED system prompt, not the raw template — this
    captures template text + $subject + taxonomy + headings in one number
    with zero extra bookkeeping. Whitespace-only edits don't invalidate
    (normalized before hashing), so a cosmetic prompt tweak doesn't force
    regenerating every already-classified paper or wiki page."""
    norm = "\n".join(line.rstrip() for line in rendered_system.split("\n")).strip()
    norm = re.sub(r"[ \t]+", " ", norm)
    return f"{PROMPT_SCHEMA_VERSION}-{hashlib.sha256(norm.encode()).hexdigest()[:10]}"
