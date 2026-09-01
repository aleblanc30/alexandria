from unittest.mock import AsyncMock

import httpx
import pytest

from pka.card_summary import SUMMARY_MAX_LEN
from pka.ingestion.fetcher import _fetch_one
from pka.ingestion.search_url import (
    is_search_engine_host,
    parse_search_url,
    search_url_result,
)


class TestNoNetwork:
    @pytest.mark.asyncio
    async def test_search_url_never_calls_http(self):
        """The whole point: a search URL is resolved without any request."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.side_effect = AssertionError("should never fetch a search URL")
        result = await _fetch_one(
            mock_client, doc_id=1, url="https://www.google.com/search?q=dark+matter"
        )
        assert result.status == "fetched"
        mock_client.get.assert_not_called()

    def test_search_url_result_sets_no_http_status(self):
        result = search_url_result(1, "https://www.google.com/search?q=hello")
        assert result is not None
        assert result.http_status is None
        assert result.error_msg == "search url; card built from query, no fetch"


class TestParseTier1:
    @pytest.mark.parametrize(
        ("url", "engine", "query"),
        [
            ("https://www.google.com/search?q=dark+matter+halos", "Google", "dark matter halos"),
            ("https://google.com/search?q=hello", "Google", "hello"),
            (
                "https://scholar.google.com/scholar?q=general+relativity",
                "Google Scholar",
                "general relativity",
            ),
            ("https://www.bing.com/search?q=rust+async", "Bing", "rust async"),
            (
                "https://www.bing.com/images/search?q=cats",
                "Bing",
                "cats",
            ),
            ("https://duckduckgo.com/?q=privacy+tools", "DuckDuckGo", "privacy tools"),
            ("https://html.duckduckgo.com/html?q=vpn", "DuckDuckGo", "vpn"),
            ("https://search.brave.com/search?q=open+source", "Brave", "open source"),
            ("https://www.ecosia.org/search?q=trees", "Ecosia", "trees"),
            (
                "https://www.startpage.com/sp/search?query=privacy",
                "Startpage",
                "privacy",
            ),
            ("https://www.qwant.com/?q=recipes", "Qwant", "recipes"),
            ("https://yandex.com/search/?text=moscow", "Yandex", "moscow"),
            ("https://www.baidu.com/s?wd=%E4%BD%A0%E5%A5%BD", "Baidu", "你好"),
            ("https://www.google.com/search?q=100%25+percent", "Google", "100% percent"),
        ],
    )
    def test_decodes_query(self, url, engine, query):
        parsed = parse_search_url(url)
        assert parsed is not None
        assert parsed.engine == engine
        assert parsed.query == query


class TestParseTier2:
    @pytest.mark.parametrize(
        ("url", "engine", "query"),
        [
            (
                "https://www.youtube.com/results?search_query=lofi+beats",
                "YouTube",
                "lofi beats",
            ),
            ("https://www.reddit.com/search/?q=climate", "Reddit", "climate"),
            (
                "https://www.reddit.com/r/python/search/?q=asyncio&restrict_sr=1",
                "Reddit",
                "asyncio",
            ),
            ("https://www.amazon.com/s?k=mechanical+keyboard", "Amazon", "mechanical keyboard"),
            ("https://github.com/search?q=fastapi&type=repositories", "GitHub", "fastapi"),
            (
                "https://stackoverflow.com/search?q=python+asyncio",
                "Stack Overflow",
                "python asyncio",
            ),
            (
                "https://pubmed.ncbi.nlm.nih.gov/?term=dark+matter",
                "PubMed",
                "dark matter",
            ),
            (
                "https://en.wikipedia.org/wiki/Special:Search?search=cats",
                "Wikipedia",
                "cats",
            ),
            (
                "https://en.wikipedia.org/w/index.php?search=dogs&title=Special:Search",
                "Wikipedia",
                "dogs",
            ),
        ],
    )
    def test_decodes_query(self, url, engine, query):
        parsed = parse_search_url(url)
        assert parsed is not None
        assert parsed.engine == engine
        assert parsed.query == query


class TestNegatives:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.google.com/maps/place/somewhere",
            "https://www.google.com/",
            "https://www.google.com/search",
            "https://www.google.com/search?q=",
            "https://www.google.com/search?q=%20%20",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.reddit.com/r/python/comments/abc123/some_thread/",
            "https://duckduckgo.com/",
        ],
    )
    def test_not_a_search_url(self, url):
        assert parse_search_url(url) is None
        assert search_url_result(1, url) is None


class TestDispatchPrecedence:
    @pytest.mark.asyncio
    async def test_youtube_search_wins_over_youtube_oembed(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.side_effect = AssertionError("should not call oEmbed for a search URL")
        result = await _fetch_one(
            mock_client,
            doc_id=1,
            url="https://www.youtube.com/results?search_query=lofi",
        )
        assert result.status == "fetched"
        assert "YouTube search" in (result.title or "")
        mock_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_wikipedia_special_search_produces_a_card_not_skipped(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.side_effect = AssertionError("should not call MediaWiki for a search URL")
        result = await _fetch_one(
            mock_client,
            doc_id=1,
            url="https://en.wikipedia.org/wiki/Special:Search?search=cats",
        )
        assert result.status == "fetched"
        assert result.status != "skipped"
        mock_client.get.assert_not_called()


class TestCardShape:
    def test_title_summary_and_text(self):
        result = search_url_result(1, "https://www.google.com/search?q=dark+matter")
        assert result is not None
        assert result.title == "dark matter — Google search"
        assert result.card_summary == 'Saved Google search for "dark matter".'
        assert result.text == "dark matter\n\nGoogle search"
        assert result.status == "fetched"

    def test_truncates_pathological_query(self):
        long_query = "a" * 5000
        result = search_url_result(1, f"https://www.google.com/search?q={long_query}")
        assert result is not None
        assert result.title is not None
        assert len(result.title) <= SUMMARY_MAX_LEN + len(" — Google search")


class TestIsSearchEngineHost:
    @pytest.mark.parametrize(
        "domain",
        ["google.com", "www.bing.com", "duckduckgo.com", "search.brave.com", "www.amazon.com"],
    )
    def test_known_hosts(self, domain):
        assert is_search_engine_host(f"https://{domain}/")

    def test_unknown_host(self):
        assert not is_search_engine_host("https://example.com/")


class TestConfigToggle:
    def test_disabled_falls_through(self, monkeypatch):
        from pka.ingestion import search_url as search_url_module

        monkeypatch.setattr(search_url_module.cfg, "search_url_cards", False)
        assert search_url_result(1, "https://www.google.com/search?q=hello") is None
