"""Corpus-agnostic text helpers shared across pipeline stages.

<- kb/config.py:77-82. safe_bibcode() (kb/config.py:72-74) is NOT here — it's
a wasp12b-specific identifier-encoding rule, not a generic text helper, and
moves into the wasp12b adapter in the papertrail migration's Step 9 (Seam 1:
identity/citation style).
"""
from __future__ import annotations

import re


def slugify(value: str) -> str:
    """Single slug rule for topic-page filenames and cross-links."""
    slug = value.strip().lower().replace("/", "-")
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")
