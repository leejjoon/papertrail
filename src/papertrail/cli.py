"""papertrail console script.

`serve` (webapp) and `run-all`/`lint`/`doctor` land as later migration steps
wire them up — see .claude/plans/i-would-like-to-stateless-hellman.md.
"""
import sys

_SUBCOMMANDS = {
    "sections": ("papertrail.pipeline.sections", "Stage A: Marker JSON -> section-tree JSON"),
    "classify": ("papertrail.pipeline.classify", "Stage B: LLM taxonomy classification"),
    "paper-pages": ("papertrail.pipeline.paper_pages", "Stage C: cited wiki paper pages"),
    "topic-pages": ("papertrail.pipeline.topic_pages", "Stage D: cross-paper topic pages"),
    "index": ("papertrail.pipeline.index", "Stage E: build kb.sqlite"),
    "mcp": ("papertrail.mcp.server", "run the MCP server (stdio, or HTTP via MCP_TRANSPORT)"),
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in _SUBCOMMANDS:
        names = ", ".join(_SUBCOMMANDS)
        print(f"usage: papertrail <command> [args...]\ncommands: {names}", file=sys.stderr)
        raise SystemExit(2)

    command = sys.argv[1]
    rest = sys.argv[2:]
    mod_path, _ = _SUBCOMMANDS[command]
    import importlib
    module = importlib.import_module(mod_path)
    module.main(rest)


if __name__ == "__main__":
    main()
