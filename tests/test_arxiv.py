"""Tests for arXiv URL parsing, Atom parsing, and fetch handler."""
from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from pka.ingestion.arxiv import (
    fetch_arxiv_paper,
    is_arxiv_url,
    normalize_arxiv_id,
    parse_arxiv_atom,
    parse_arxiv_url,
)

_ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2301.00001v1</id>
    <title>Attention Is All You Need Again</title>
    <summary>We revisit transformer architectures for language modeling.</summary>
    <author><name>Alice Smith</name></author>
    <author><name>Bob Jones</name></author>
    <category term="cs.CL"/>
    <category term="cs.LG"/>
  </entry>
</feed>
"""


class TestParseArxivUrl:
    def test_abs_url(self):
        assert parse_arxiv_url("https://arxiv.org/abs/2301.00001") == "2301.00001"

    def test_abs_with_version(self):
        assert parse_arxiv_url("https://arxiv.org/abs/2301.00001v2") == "2301.00001"

    def test_pdf_url(self):
        assert parse_arxiv_url("https://arxiv.org/pdf/2301.00001.pdf") == "2301.00001"

    def test_html_url(self):
        assert parse_arxiv_url("https://arxiv.org/html/2301.00001") == "2301.00001"

    def test_www_host(self):
        assert parse_arxiv_url("https://www.arxiv.org/abs/2301.00001") == "2301.00001"

    def test_non_arxiv_returns_none(self):
        assert parse_arxiv_url("https://example.com/abs/2301.00001") is None

    def test_is_arxiv_url(self):
        assert is_arxiv_url("https://arxiv.org/abs/1")
        assert not is_arxiv_url("https://example.com/")


class TestNormalizeArxivId:
    def test_strips_version(self):
        assert normalize_arxiv_id("2301.00001v3") == "2301.00001"


class TestParseArxivAtom:
    def test_parses_entry(self):
        meta = parse_arxiv_atom(_ATOM_XML)
        assert meta is not None
        assert meta.arxiv_id == "2301.00001"
        assert meta.title == "Attention Is All You Need Again"
        assert "transformer" in meta.abstract.lower()
        assert meta.authors == ["Alice Smith", "Bob Jones"]
        assert "cs.CL" in meta.categories

    def test_invalid_xml_returns_none(self):
        assert parse_arxiv_atom("not xml") is None


class TestFetchArxivPaper:
    @pytest.mark.asyncio
    async def test_fetches_metadata_and_pdf(self, monkeypatch):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        api_resp = httpx.Response(200, text=_ATOM_XML, request=httpx.Request("GET", "http://x"))
        mock_client.get.return_value = api_resp

        async def fake_pdf(client, arxiv_id):
            return "Full paper body with enough text for embedding purposes here.", 200, None

        monkeypatch.setattr("pka.ingestion.arxiv._fetch_arxiv_pdf_text", fake_pdf)

        result = await fetch_arxiv_paper(
            mock_client,
            doc_id=1,
            url="https://arxiv.org/abs/2301.00001",
        )

        assert result is not None
        assert result.status == "fetched"
        assert result.title == "Attention Is All You Need Again"
        assert result.card_summary == "We revisit transformer architectures for language modeling."
        assert "Full paper body" in (result.text or "")
        assert result.error_msg == "fetched via arxiv api"

    @pytest.mark.asyncio
    async def test_abstract_only_when_pdf_fails(self, monkeypatch):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        api_resp = httpx.Response(200, text=_ATOM_XML, request=httpx.Request("GET", "http://x"))
        pdf_resp = httpx.Response(404, request=httpx.Request("GET", "http://x"))
        mock_client.get.side_effect = [api_resp, pdf_resp]

        result = await fetch_arxiv_paper(
            mock_client,
            doc_id=2,
            url="https://arxiv.org/abs/2301.00001",
        )

        assert result is not None
        assert result.status == "fetched"
        assert result.title == "Attention Is All You Need Again"
        assert result.card_summary == "We revisit transformer architectures for language modeling."
        assert "abstract only" in (result.error_msg or "")

    @pytest.mark.asyncio
    async def test_non_arxiv_returns_none(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        assert await fetch_arxiv_paper(mock_client, 1, "https://example.com") is None
