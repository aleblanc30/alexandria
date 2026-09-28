"""``rate_limited_get`` and ``fetch_pdf_text``: the GET, failure mapping and PDF
leg the per-site handlers share."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from pka.config import settings
from pka.ingestion.fetch_base import FetchResult, fetch_pdf_text, rate_limited_get


def _client(*, status: int | None = None, raises: Exception | None = None) -> MagicMock:
    client = MagicMock()
    if raises is not None:
        client.get = AsyncMock(side_effect=raises)
    else:
        client.get = AsyncMock(return_value=MagicMock(status_code=status, content=b"%PDF-"))
    return client


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr("pka.ingestion.fetch_base._limiter.wait", AsyncMock())


async def test_success_returns_the_response_and_its_status():
    client = _client(status=200)
    resp, status, err = await rate_limited_get(client, "https://example.org/a")
    assert resp is client.get.return_value
    assert (status, err) == (200, None)


@pytest.mark.parametrize(("pdf", "expected"), [(False, "HTTP 404"), (True, "pdf HTTP 404")])
async def test_an_error_status_is_reported_with_it(pdf, expected):
    resp, status, err = await rate_limited_get(_client(status=404), "https://x.org/a", pdf=pdf)
    assert (resp, status, err) == (None, 404, expected)


@pytest.mark.parametrize(("pdf", "expected"), [(False, "timeout"), (True, "pdf timeout")])
async def test_a_timeout_has_no_status(pdf, expected):
    client = _client(raises=httpx.ReadTimeout("slow"))
    assert await rate_limited_get(client, "https://x.org/a", pdf=pdf) == (None, None, expected)


async def test_a_request_error_carries_its_message():
    client = _client(raises=httpx.ConnectError("refused"))
    assert await rate_limited_get(client, "https://x.org/a") == (None, None, "refused")


async def test_headers_are_sent_only_when_given():
    client = _client(status=200)
    await rate_limited_get(client, "https://x.org/a")
    assert "headers" not in client.get.call_args.kwargs

    await rate_limited_get(client, "https://x.org/a", headers={"Accept": "application/json"})
    assert client.get.call_args.kwargs["headers"] == {"Accept": "application/json"}


async def test_the_domain_slot_is_awaited_first(monkeypatch):
    wait = AsyncMock()
    monkeypatch.setattr("pka.ingestion.fetch_base._limiter.wait", wait)
    await rate_limited_get(_client(status=200), "https://x.org/a")
    wait.assert_awaited_once_with("https://x.org/a")


class TestFetchPdfText:
    async def test_extracted_text_is_returned_with_the_status(self, monkeypatch):
        monkeypatch.setattr(
            "pka.ingestion.fetch_base._fetch_pdf_result",
            lambda doc_id, url, body, status: FetchResult(
                doc_id, url, "fetched", "body", status, None
            ),
        )
        client = _client(status=200)
        assert await fetch_pdf_text(client, "https://x.org/a.pdf") == ("body", 200, None)
        # The PDF leg gets the PDF read timeout, not the page one.
        assert client.get.call_args.kwargs["timeout"].read == settings.fetch_pdf_timeout_seconds

    async def test_an_extraction_failure_keeps_its_reason(self, monkeypatch):
        monkeypatch.setattr(
            "pka.ingestion.fetch_base._fetch_pdf_result",
            lambda doc_id, url, body, status: FetchResult(
                doc_id, url, "no_text_layer", None, status, "scanned pdf"
            ),
        )
        assert await fetch_pdf_text(_client(status=200), "https://x.org/a.pdf") == (
            None,
            200,
            "scanned pdf",
        )

    async def test_an_empty_extraction_has_a_default_reason(self, monkeypatch):
        monkeypatch.setattr(
            "pka.ingestion.fetch_base._fetch_pdf_result",
            lambda doc_id, url, body, status: FetchResult(doc_id, url, "fetched", "", status, None),
        )
        assert await fetch_pdf_text(_client(status=200), "https://x.org/a.pdf") == (
            None,
            200,
            "pdf extraction failed",
        )

    async def test_a_failed_download_is_reported_as_the_pdf_leg(self):
        assert await fetch_pdf_text(_client(status=503), "https://x.org/a.pdf") == (
            None,
            503,
            "pdf HTTP 503",
        )
