"""CLI entry point for serving a corpus in the packaged browser reader."""
from __future__ import annotations

import argparse

import uvicorn

from ..config import load_settings
from .app import create_app


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve a Papertrail corpus in the browser.")
    parser.add_argument("--corpus", required=True, metavar="PATH")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--root-path", default="")
    return parser


def main(argv=None) -> None:
    args = build_arg_parser().parse_args(argv)
    settings = load_settings(args.corpus)
    if not settings.sqlite_path.is_file():
        raise SystemExit(
            f"search index not found at {settings.sqlite_path}; run papertrail ingest-pdf or index first")
    uvicorn.run(create_app(settings, root_path=args.root_path), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
