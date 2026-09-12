from __future__ import annotations

import json

import pytest

from papertrail.datalab import DatalabConversionError, convert_pdf


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Session:
    def __init__(self, polls):
        self.polls = iter(polls)
        self.submission = None

    def post(self, url, **kwargs):
        self.submission = (url, kwargs)
        return Response({"success": True, "request_check_url": "https://status/123"})

    def get(self, url, **kwargs):
        return Response(next(self.polls))


def test_convert_pdf_polls_and_normalizes_serialized_json(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    document = {"children": []}
    session = Session([
        {"status": "processing"},
        {"status": "complete", "success": True, "page_count": 1,
         "json": json.dumps(document)},
    ])

    result = convert_pdf(pdf, "secret", poll_interval=0, session=session)

    assert result["json"] == document
    _, request = session.submission
    assert request["headers"] == {"X-API-Key": "secret"}
    assert request["data"]["output_format"] == "json"
    assert request["data"]["add_block_ids"] == "true"
    assert request["data"]["disable_image_extraction"] == "false"


def test_convert_pdf_reports_failed_poll(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-test")
    session = Session([{"status": "failed", "error": "bad document"}])

    with pytest.raises(DatalabConversionError, match="bad document"):
        convert_pdf(pdf, "secret", poll_interval=0, session=session)
