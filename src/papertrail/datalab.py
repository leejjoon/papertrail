"""Small client for Datalab's asynchronous document-conversion API."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

CONVERT_URL = "https://www.datalab.to/api/v1/convert"


class DatalabConversionError(RuntimeError):
    """Raised when Datalab rejects, fails, or times out on a conversion."""


def _session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(("GET", "POST")),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def convert_pdf(pdf_path: Path, api_key: str, *, mode: str = "balanced",
                poll_interval: float = 2, timeout: float = 15 * 60,
                session: requests.Session | None = None) -> dict:
    """Convert one PDF to Marker JSON and return the complete poll response."""
    if not api_key:
        raise DatalabConversionError("DATALAB_API_KEY is missing or empty")
    client = session or _session()
    headers = {"X-API-Key": api_key}
    try:
        with pdf_path.open("rb") as fh:
            response = client.post(
                CONVERT_URL,
                headers=headers,
                files={"file": (pdf_path.name, fh, "application/pdf")},
                data={
                    "output_format": "json",
                    "mode": mode,
                    "paginate": "false",
                    "add_block_ids": "true",
                    "disable_image_extraction": "false",
                },
                timeout=120,
            )
        response.raise_for_status()
        submitted = response.json()
    except requests.RequestException as exc:
        raise DatalabConversionError(f"Datalab submission failed: {exc}") from exc
    if not submitted.get("success", True):
        raise DatalabConversionError(submitted.get("error") or "Datalab rejected the request")
    check_url = submitted.get("request_check_url")
    if not check_url:
        raise DatalabConversionError("Datalab response did not include request_check_url")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        try:
            response = client.get(check_url, headers=headers, timeout=60)
            response.raise_for_status()
            result = response.json()
        except requests.RequestException as exc:
            raise DatalabConversionError(f"Datalab status check failed: {exc}") from exc
        status = result.get("status")
        if status == "complete":
            if not result.get("success", True):
                raise DatalabConversionError(result.get("error") or "Datalab conversion failed")
            document = result.get("json")
            if isinstance(document, str):
                try:
                    document = json.loads(document)
                except json.JSONDecodeError as exc:
                    raise DatalabConversionError("Datalab returned invalid serialized JSON") from exc
                result["json"] = document
            if not isinstance(document, dict):
                raise DatalabConversionError("Datalab result did not contain a JSON document tree")
            return result
        if status == "failed":
            raise DatalabConversionError(result.get("error") or "Datalab conversion failed")
    raise DatalabConversionError(f"Datalab polling timed out after {timeout:g}s")
