"""Tests for bioRxiv URL parsing, JSON parsing, and fetch handler."""
from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from pka.ingestion.biorxiv import (
    fetch_biorxiv_paper,
    is_biorxiv_url,
    parse_biorxiv_detail,
    parse_biorxiv_url,
)

_BIORXIV_JSON = {
    "messages": [{"status": "ok", "count": 1}],
    "collection": [{
        "doi": "10.1101/2024.01.16.575895",
        "title": "A neural circuit for reward",
        "authors": "Smith, A.; Jones, B.",
        "abstract": "We map dopamine neurons in the ventral tegmental area.",
        "version": "2",
        "category": "neuroscience",
    }],
}


class TestParseBiorxivUrl:
    def test_content_url_with_version(self):
        assert parse_biorxiv_url(
            "https://www.biorxiv.org/content/10.1101/2024.01.16.575895v2"
        ) == ("10.1101/2024.01.16.575895", 2)

    def test_full_pdf_url(self):
        assert parse_biorxiv_url(
            "https://www.biorxiv.org/content/10.1101/2024.01.16.575895v1.full.pdf"
        ) == ("10.1101/2024.01.16.575895", 1)

    def test_early_content_path(self):
        assert parse_biorxiv_url(
            "https://www.biorxiv.org/content/early/2024/01/20/2024.01.16.575895"
        ) == ("10.1101/2024.01.16.575895", None)

    def test_non_biorxiv_returns_none(self):
        assert parse_biorxiv_url("https://example.com/content/10.1101/1") is None

    def test_is_biorxiv_url(self):
        assert is_biorxiv_url("https://biorxiv.org/content/10.1101/1")
        assert not is_biorxiv_url("https://example.com/")


class TestParseBiorxivDetail:
    def test_parses_collection(self):
        meta = parse_biorxiv_detail(_BIORXIV_JSON)
        assert meta is not None
        assert meta.doi == "10.1101/2024.01.16.575895"
        assert meta.title == "A neural circuit for reward"
        assert meta.version == 2
        assert "dopamine" in meta.abstract

    def test_empty_collection_returns_none(self):
        assert parse_biorxiv_detail({"collection": []}) is None


class TestFetchBiorxivPaper:
    @pytest.mark.asyncio
    async def test_fetches_metadata_and_pdf(self, monkeypatch):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        api_resp = httpx.Response(
            200,
            json=_BIORXIV_JSON,
            request=httpx.Request("GET", "http://x"),
        )
        mock_client.get.return_value = api_resp

        async def fake_pdf(client, doi, version):
            return "BioRxiv PDF body with enough extracted text for embedding.", 200, None

        monkeypatch.setattr("pka.ingestion.biorxiv._fetch_biorxiv_pdf_text", fake_pdf)

        result = await fetch_biorxiv_paper(
            mock_client,
            doc_id=1,
            url="https://www.biorxiv.org/content/10.1101/2024.01.16.575895v2",
        )

        assert result is not None
        assert result.status == "fetched"
        assert result.title == "A neural circuit for reward"
        assert result.card_summary == "We map dopamine neurons in the ventral tegmental area."
        assert "BioRxiv PDF body" in (result.text or "")
        assert result.doi == "10.1101/2024.01.16.575895"
        import json
        assert json.loads(result.authors_json) == ["Smith, A.", "Jones, B."]

    @pytest.mark.asyncio
    async def test_non_biorxiv_returns_none(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        assert await fetch_biorxiv_paper(mock_client, 1, "https://example.com") is None
