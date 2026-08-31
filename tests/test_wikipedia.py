import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from pka.ingestion.fetcher import _fetch_one
from pka.ingestion.wikipedia import (
    fetch_via_wikipedia_api,
    fetch_wikipedia_with_retries,
    is_wikipedia_special,
    parse_wikipedia_url,
)


def _api_response(
    extract: str | None = "Python is a programming language.",
    *,
    missing: bool = False,
    status: int = 200,
) -> MagicMock:
    if missing:
        pages = {"-1": {"ns": 0, "title": "Missing", "missing": ""}}
    else:
        pages = {
            "123": {
                "pageid": 123,
                "ns": 0,
                "title": "Python",
                "extract": extract or "",
            }
        }
    payload = {"batchcomplete": "", "query": {"pages": pages}}
    body = json.dumps(payload)
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.text = body
    resp.content = body.encode("utf-8")
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = payload
    return resp


class TestParseWikipediaUrl:
    def test_wiki_path(self):
        assert parse_wikipedia_url(
            "https://en.wikipedia.org/wiki/Python_(programming_language)"
        ) == ("en", "Python_(programming_language)")

    def test_percent_encoding(self):
        assert parse_wikipedia_url("https://en.wikipedia.org/wiki/Foo%20Bar") == (
            "en",
            "Foo_Bar",
        )

    def test_mobile_host(self):
        assert parse_wikipedia_url("https://en.m.wikipedia.org/wiki/Foo") == ("en", "Foo")

    def test_index_php_title(self):
        assert parse_wikipedia_url("https://en.wikipedia.org/w/index.php?title=Foo&oldid=123") == (
            "en",
            "Foo",
        )

    def test_fragment_stripped(self):
        assert parse_wikipedia_url("https://en.wikipedia.org/wiki/Foo#section") == (
            "en",
            "Foo",
        )

    def test_non_wikipedia_returns_none(self):
        assert parse_wikipedia_url("https://example.com/wiki/Foo") is None

    def test_special_page_returns_none(self):
        assert parse_wikipedia_url("https://en.wikipedia.org/wiki/Special:Search") is None

    def test_trailing_slash_stripped(self):
        assert parse_wikipedia_url("https://en.wikipedia.org/wiki/Foo/") == ("en", "Foo")

    def test_other_language(self):
        assert parse_wikipedia_url("https://fr.wikipedia.org/wiki/Paris") == ("fr", "Paris")


class TestIsWikipediaSpecial:
    def test_special_wiki_path(self):
        assert is_wikipedia_special("https://en.wikipedia.org/wiki/Special:Search") is True

    def test_special_index_php(self):
        assert (
            is_wikipedia_special("https://en.wikipedia.org/w/index.php?title=Special:Random")
            is True
        )

    def test_article_is_not_special(self):
        assert is_wikipedia_special("https://en.wikipedia.org/wiki/Python") is False

    def test_non_wikipedia_is_not_special(self):
        assert is_wikipedia_special("https://example.com/wiki/Special:Search") is False


class TestFetchViaWikipediaApi:
    @pytest.mark.asyncio
    async def test_fetches_extract(self):
        url = "https://en.wikipedia.org/wiki/Python"
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _api_response()

        result = await fetch_via_wikipedia_api(mock_client, doc_id=1, url=url)

        assert result is not None
        assert result.status == "fetched"
        assert result.text == "Python is a programming language."
        assert mock_client.post.call_args.args[0].endswith("/w/api.php")
        assert mock_client.post.call_args.kwargs["data"]["titles"] == "Python"

    @pytest.mark.asyncio
    async def test_missing_page_fails(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _api_response(missing=True)

        result = await fetch_via_wikipedia_api(
            mock_client,
            doc_id=1,
            url="https://en.wikipedia.org/wiki/MissingPage",
        )

        assert result is not None
        assert result.status == "unfetchable"
        assert "missing or empty" in (result.error_msg or "")

    @pytest.mark.asyncio
    async def test_http_error_fails(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _api_response(status=500)

        result = await fetch_via_wikipedia_api(
            mock_client,
            doc_id=1,
            url="https://en.wikipedia.org/wiki/Python",
        )

        assert result is not None
        assert result.status == "unfetchable"
        assert result.http_status == 500


class TestFetchWikipediaWithRetries:
    @pytest.mark.asyncio
    async def test_retries_then_succeeds(self, monkeypatch):
        monkeypatch.setattr("pka.config.settings.fetch_wikipedia_max_retries", 2)
        monkeypatch.setattr("pka.config.settings.fetch_wikipedia_retry_delay_seconds", 0.01)

        calls = {"n": 0}

        async def route_post(url, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                return _api_response(extract="")
            return _api_response()

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.side_effect = route_post

        async def noop_wait(url: str) -> None:
            return None

        monkeypatch.setattr("pka.ingestion.wikipedia._limiter.wait", noop_wait)
        with patch("pka.ingestion.wikipedia.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await fetch_wikipedia_with_retries(
                mock_client,
                doc_id=1,
                url="https://en.wikipedia.org/wiki/Python",
            )

        assert result.status == "fetched"
        assert calls["n"] == 3
        assert sleep.await_count == 2

    @pytest.mark.asyncio
    async def test_exhausted_retries_unfetchable(self, monkeypatch):
        monkeypatch.setattr("pka.config.settings.fetch_wikipedia_max_retries", 2)
        monkeypatch.setattr("pka.config.settings.fetch_wikipedia_retry_delay_seconds", 0.01)

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _api_response(extract="")

        async def noop_wait(url: str) -> None:
            return None

        monkeypatch.setattr("pka.ingestion.wikipedia._limiter.wait", noop_wait)
        with patch("pka.ingestion.wikipedia.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await fetch_wikipedia_with_retries(
                mock_client,
                doc_id=1,
                url="https://en.wikipedia.org/wiki/Python",
            )

        assert result.status == "unfetchable"
        assert mock_client.post.call_count == 3
        assert sleep.await_count == 2


class TestFetchOneWikipedia:
    @pytest.mark.asyncio
    async def test_uses_api_not_article_html(self):
        url = "https://en.wikipedia.org/wiki/Python"
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _api_response()

        result = await _fetch_one(mock_client, doc_id=1, url=url)

        assert result.status == "fetched"
        assert result.text is not None
        assert mock_client.post.call_count == 1
        called_url = mock_client.post.call_args.args[0]
        assert "/w/api.php" in called_url
        assert called_url != url
        mock_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_special_page_skipped(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        result = await _fetch_one(
            mock_client,
            doc_id=1,
            url="https://en.wikipedia.org/wiki/Special:Search",
        )

        assert result.status == "skipped"
        assert result.error_msg == "wikipedia special page"
        mock_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_unfetchable_after_retries(self, monkeypatch):
        monkeypatch.setattr("pka.config.settings.fetch_wikipedia_max_retries", 2)
        monkeypatch.setattr("pka.config.settings.fetch_wikipedia_retry_delay_seconds", 0.01)

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = _api_response(extract="")

        async def noop_wait(url: str) -> None:
            return None

        monkeypatch.setattr("pka.ingestion.wikipedia._limiter.wait", noop_wait)
        with patch("pka.ingestion.wikipedia.asyncio.sleep", new_callable=AsyncMock):
            result = await _fetch_one(
                mock_client,
                doc_id=1,
                url="https://en.wikipedia.org/wiki/Missing",
            )

        assert result.status == "unfetchable"
        assert mock_client.post.call_count == 3
        mock_client.get.assert_not_called()
