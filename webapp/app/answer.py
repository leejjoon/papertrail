"""Compatibility shim — migration Step 3. Real implementation moved to
src/papertrail/core/citations.py. See webapp/app/registry.py's shim
docstring. (Renamed answer.py -> citations.py in the move — "citations" is
what the module actually validates/enriches; kept as `answer` here only for
back-compat with existing imports.)"""
from papertrail.core.citations import (  # noqa: F401
    InvalidCitationsError,
    find_uncited_author_year_mentions,
    normalize_payload,
    validate_and_enrich,
)
