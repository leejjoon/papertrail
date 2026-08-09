"""papertrail: Marker JSON -> section tree -> classification -> wiki ->
SQLite/FTS -> web UI + MCP, for any corpus of documents that cite each
other.

Zero import-time side effects by design (no path resolution, no directory
creation, no client construction) — see docs/spec/ and
.claude/plans/i-would-like-to-stateless-hellman.md for the extraction this
package is being built up into, step by step, from kb/ and webapp/.
"""

__version__ = "0.1.0"
