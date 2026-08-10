"""papertrail console script.

Only `mcp` is wired up so far (migration Step 5). The pipeline subcommands
(sections, classify, paper-pages, topic-pages, index, run-all) and `serve`
land as code moves in from kb/*.py and webapp/*.py in later steps — see
.claude/plans/i-would-like-to-stateless-hellman.md.
"""
import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="papertrail")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mcp", help="run the MCP server (stdio, or HTTP via MCP_TRANSPORT)")

    args, _ = parser.parse_known_args()

    if args.command == "mcp":
        from .mcp.server import main as mcp_main
        mcp_main()
        return

    print(f"papertrail: unknown command {args.command!r}", file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
