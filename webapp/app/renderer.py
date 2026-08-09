"""Compatibility shim — migration Step 3. Real implementation moved to
src/papertrail/core/renderer.py. See webapp/app/registry.py's shim docstring."""
from papertrail.core.renderer import render_doc_html, render_node_html  # noqa: F401
