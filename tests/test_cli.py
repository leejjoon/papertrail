from papertrail.cli import _SUBCOMMANDS


def test_pdf_workflow_commands_are_registered():
    assert _SUBCOMMANDS["ingest-pdf"][0] == "papertrail.pipeline.ingest_pdf"
    assert _SUBCOMMANDS["serve"][0] == "papertrail.web.serve"
