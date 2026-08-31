"""Tests for Amazon book page URL detection and HTML extraction."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import sqlalchemy as sa

from pka.constants import FetchStatus, Source
from pka.db.queries import get_engine, init_db, upsert_document
from pka.db.schema import documents
from pka.ingestion.amazon import (
    AmazonBook,
    extract_amazon_book,
    is_amazon_book_url,
    is_amazon_host,
)
from pka.ingestion.fetcher import FetchResult, _fetch_one, _persist_fetch_result


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()


_BOOK_HTML = """
<html>
<head>
  <meta name="description" content="Meta fallback description for the book." />
</head>
<body>
  <span id="productTitle">The Great Gatsby</span>
  <div id="bookDescription_feature_div">
    <span>A portrait of the Jazz Age and the American Dream,
    following Jay Gatsby's obsession with Daisy Buchanan.</span>
  </div>
</body>
</html>
"""

_PRODUCT_DESC_HTML = """
<html>
<body>
  <span id="productTitle">Clean Code</span>
  <div id="productDescription">
    <p>Even bad code can function. But if code is not clean, it can bring a development
    organization to its knees.</p>
  </div>
</body>
</html>
"""

_META_ONLY_HTML = """
<html>
<head>
  <meta name="description" content="A novel about ambition and loss in 1920s America." />
</head>
<body>
  <span id="productTitle">Some Novel</span>
</body>
</html>
"""


def _html_response(body: str, status: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.text = body
    resp.content = body.encode("utf-8")
    resp.headers = {"content-type": "text/html"}
    return resp


class TestAmazonHost:
    def test_amazon_com(self):
        assert is_amazon_host("https://www.amazon.com/dp/B012345678")

    def test_amazon_co_uk(self):
        assert is_amazon_host("https://www.amazon.co.uk/dp/B012345678")

    def test_amazon_ca(self):
        assert is_amazon_host("https://amazon.ca/dp/B012345678")

    def test_non_amazon(self):
        assert not is_amazon_host("https://example.com/dp/B012345678")

    def test_amazon_search_not_book_url(self):
        assert is_amazon_host("https://www.amazon.com/s?k=books")
        assert not is_amazon_book_url("https://www.amazon.com/s?k=books")


class TestAmazonBookUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.amazon.com/dp/B012345678",
            "https://www.amazon.com/Some-Book-Title/dp/B012345678/ref=sr_1_1",
            "https://www.amazon.co.uk/gp/product/B012345678",
            "https://www.amazon.com/gp/aw/d/B012345678",
        ],
    )
    def test_valid_product_urls(self, url):
        assert is_amazon_book_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.amazon.com/s?k=books",
            "https://example.com/dp/B012345678",
            "https://www.amazon.com/",
        ],
    )
    def test_invalid_urls(self, url):
        assert not is_amazon_book_url(url)


class TestExtractAmazonBook:
    def test_book_description_div(self):
        book = extract_amazon_book(_BOOK_HTML)
        assert book == AmazonBook(
            title="The Great Gatsby",
            summary=(
                "A portrait of the Jazz Age and the American Dream, "
                "following Jay Gatsby's obsession with Daisy Buchanan."
            ),
        )

    def test_product_description_fallback(self):
        book = extract_amazon_book(_PRODUCT_DESC_HTML)
        assert book is not None
        assert book.title == "Clean Code"
        assert "bad code can function" in book.summary

    def test_meta_description_fallback(self):
        book = extract_amazon_book(_META_ONLY_HTML)
        assert book is not None
        assert book.title == "Some Novel"
        assert book.summary == "A novel about ambition and loss in 1920s America."

    def test_prefers_book_description_over_meta(self):
        book = extract_amazon_book(_BOOK_HTML)
        assert book is not None
        assert "Jazz Age" in book.summary
        assert "Meta fallback" not in book.summary

    def test_missing_title_returns_none(self):
        html = "<html><div id='bookDescription_feature_div'>Summary only.</div></html>"
        assert extract_amazon_book(html) is None

    def test_missing_summary_returns_none(self):
        html = "<html><span id='productTitle'>Title Only</span></html>"
        assert extract_amazon_book(html) is None


class TestAmazonFetchIntegration:
    @pytest.mark.asyncio
    async def test_fetch_one_uses_amazon_handler(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _html_response(_BOOK_HTML)
        url = "https://www.amazon.com/dp/B012345678"
        result = await _fetch_one(mock_client, doc_id=1, url=url)
        assert result.status == "fetched"
        assert result.title == "The Great Gatsby"
        assert "Jazz Age" in (result.text or "")
        assert result.error_msg == "fetched via amazon book handler"

    @pytest.mark.asyncio
    async def test_fetch_one_falls_back_when_extraction_fails(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        body = "<html><body><p>" + "word " * 50 + "</p></body></html>"
        mock_client.get.return_value = _html_response(body)
        url = "https://www.amazon.com/dp/B012345678"
        result = await _fetch_one(mock_client, doc_id=1, url=url)
        assert result.status == "fetched"
        assert result.title is None
        assert result.text is not None

    @pytest.mark.asyncio
    async def test_persist_updates_title(self):
        doc_id = upsert_document(
            source=Source.FIREFOX,
            source_id="F-AMZ",
            title="Amazon.com: Old bookmark title",
            url_or_path="https://www.amazon.com/dp/B012345678",
            date_added=None,
            fetch_status=FetchStatus.PENDING,
        )
        _persist_fetch_result(
            FetchResult(
                doc_id,
                "https://www.amazon.com/dp/B012345678",
                "fetched",
                "A portrait of the Jazz Age.",
                200,
                "fetched via amazon book handler",
                title="The Great Gatsby",
            )
        )
        with get_engine().connect() as con:
            row = con.execute(
                sa.select(documents.c.title).where(documents.c.id == doc_id)
            ).fetchone()
        assert row[0] == "The Great Gatsby"
