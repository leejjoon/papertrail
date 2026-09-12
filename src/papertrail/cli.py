"""papertrail console script.

Each command imports its implementation lazily so optional web/Datalab/MCP
dependencies are needed only by the command that uses them.
"""
import sys

_SUBCOMMANDS = {
    "ingest-pdf": ("papertrail.pipeline.ingest_pdf", "Datalab PDF conversion + sections + index"),
    "sections": ("papertrail.pipeline.sections", "Stage A: Marker JSON -> section-tree JSON"),
    "classify": ("papertrail.pipeline.classify", "Stage B: LLM taxonomy classification"),
    "paper-pages": ("papertrail.pipeline.paper_pages", "Stage C: cited wiki paper pages"),
    "topic-pages": ("papertrail.pipeline.topic_pages", "Stage D: cross-paper topic pages"),
    "index": ("papertrail.pipeline.index", "Stage E: build kb.sqlite"),
    "serve": ("papertrail.web.serve", "run the browser reader"),
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
