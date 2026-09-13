import pytest

from papertrail.cli import _SUBCOMMANDS


def test_pdf_workflow_commands_are_registered():
    assert _SUBCOMMANDS["ingest-pdf"][0] == "papertrail.pipeline.ingest_pdf"
    assert _SUBCOMMANDS["serve"][0] == "papertrail.web.serve"


def test_mcp_http_flags_are_explicit():
    pytest.importorskip("mcp")
    from papertrail.mcp.server import build_arg_parser as build_mcp_arg_parser

    args = build_mcp_arg_parser().parse_args([
        "--corpus", "corpora/example", "--transport", "streamable-http",
        "--host", "127.0.0.1", "--port", "8013",
    ])
    assert args.transport == "streamable-http"
    assert args.host == "127.0.0.1"
    assert args.port == 8013
