"""Compatibility shim — migration Step 3.

The real implementation moved to src/papertrail/core/registry.py (tree
walkers split into core/nodes.py). Kept here, re-exporting the same names,
so webapp/app/main.py's and kb/mcp_server.py's `from .registry import ...`
/ `from app.registry import ...` keep resolving unchanged while the rest of
the extraction (.claude/plans/i-would-like-to-stateless-hellman.md) lands.
Delete once every consumer imports papertrail.core directly (Step 4/5).
"""
from papertrail.core.nodes import (  # noqa: F401
    build_toc_outline,
    concat_subtree,
    context_window,
    find_node_with_siblings,
    iter_nodes,
)
from papertrail.core.registry import (  # noqa: F401
    CorpusRegistry,
    Doc,
    _LazyDocMap,
    ads_url,
    author_year_label,
    derive_label,
    section_payload,
)
