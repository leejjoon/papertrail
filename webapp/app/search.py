"""Compatibility shim — migration Step 3. Real implementation moved to
src/papertrail/core/search.py. See webapp/app/registry.py's shim docstring."""
from papertrail.core.search import fts_escape, search_nodes  # noqa: F401
