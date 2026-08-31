"""Tests for Reddit bookmark URL parsing, JSON parsing, and fetch handler."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from pka.ingestion.reddit_bookmark import (
    RedditPermalink,
    fetch_reddit_thread,
    is_reddit_host,
    parse_reddit_listing,
    parse_reddit_permalink,
)

_SELF_POST_URL = (
    "https://www.reddit.com/r/MachineLearning/comments/abc123/a_deep_dive_into_transformers/"
)
_LINK_POST_URL = "https://www.reddit.com/r/programming/comments/def456/cool_article_about_rust/"

_SELF_POST_JSON = [
    {
        "data": {
            "children": [
                {
                    "data": {
                        "title": "A deep dive into transformers",
                        "subreddit": "MachineLearning",
                        "is_self": True,
                        "selftext": "Here is a long explanation of attention mechanisms.",
                        "url": "https://www.reddit.com/r/MachineLearning/comments/abc123/",
                    }
                }
            ]
        }
    },
    {"data": {"children": []}},
]

_LINK_POST_JSON = [
    {
        "data": {
            "children": [
                {
                    "data": {
                        "title": "Cool article about Rust",
                        "subreddit": "programming",
                        "is_self": False,
                        "selftext": "",
                        "url": "https://example.com/rust-article",
                    }
                }
            ]
        }
    },
    {
        "data": {
            "children": [
                {"kind": "t1", "data": {"author": "alice", "body": "Great read!"}},
                {"kind": "t1", "data": {"author": "AutoModerator", "body": "Remember the rules."}},
                {"kind": "t1", "data": {"author": "bob", "body": "[deleted]"}},
                {"kind": "t1", "data": {"author": "carol", "body": "Agreed, well written."}},
                {"kind": "more", "data": {"children": []}},
            ]
        }
    },
]


class TestParseRedditPermalink:
    def test_self_post_url(self):
        assert parse_reddit_permalink(_SELF_POST_URL) == RedditPermalink(
            subreddit="MachineLearning",
            post_id="abc123",
            slug="a_deep_dive_into_transformers",
        )

    def test_old_reddit_host(self):
        url = "https://old.reddit.com/r/programming/comments/def456/cool_article_about_rust/"
        result = parse_reddit_permalink(url)
        assert result is not None
        assert result.subreddit == "programming"
        assert result.post_id == "def456"

    def test_np_reddit_host(self):
        url = "https://np.reddit.com/r/programming/comments/def456/cool_article/"
        assert parse_reddit_permalink(url) is not None

    def test_no_slug(self):
        result = parse_reddit_permalink("https://www.reddit.com/r/programming/comments/def456/")
        assert result is not None
        assert result.slug is None

    def test_redd_it_short_link(self):
        result = parse_reddit_permalink("https://redd.it/abc123")
        assert result == RedditPermalink(subreddit=None, post_id="abc123", slug=None)

    def test_subreddit_front_page_returns_none(self):
        assert parse_reddit_permalink("https://www.reddit.com/r/programming/") is None

    def test_user_profile_returns_none(self):
        assert parse_reddit_permalink("https://www.reddit.com/user/someuser/") is None

    def test_non_reddit_returns_none(self):
        assert parse_reddit_permalink("https://example.com/r/x/comments/1/") is None

    def test_is_reddit_host(self):
        assert is_reddit_host("https://www.reddit.com/")
        assert is_reddit_host("https://redd.it/")
        assert not is_reddit_host("https://example.com/")


class TestParseRedditListing:
    def test_self_post(self):
        thread = parse_reddit_listing(_SELF_POST_JSON)
        assert thread is not None
        assert thread.title == "A deep dive into transformers"
        assert thread.subreddit == "MachineLearning"
        assert thread.is_self is True
        assert "attention mechanisms" in thread.selftext
        assert thread.external_url is None
        assert thread.comments == []

    def test_link_post_falls_back_to_comments(self):
        thread = parse_reddit_listing(_LINK_POST_JSON)
        assert thread is not None
        assert thread.is_self is False
        assert thread.selftext == ""
        assert thread.external_url == "https://example.com/rust-article"
        assert thread.comments == ["Great read!", "Agreed, well written."]

    def test_missing_title_returns_none(self):
        data = [{"data": {"children": [{"data": {"subreddit": "x"}}]}}]
        assert parse_reddit_listing(data) is None

    def test_empty_list_returns_none(self):
        assert parse_reddit_listing([]) is None

    def test_malformed_structure_returns_none(self):
        assert parse_reddit_listing({"not": "a list"}) is None
        assert parse_reddit_listing([{"data": {}}]) is None


class TestFetchRedditThread:
    @pytest.mark.asyncio
    async def test_fetches_self_post(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = httpx.Response(
            200, json=_SELF_POST_JSON, request=httpx.Request("GET", "http://x")
        )

        result = await fetch_reddit_thread(mock_client, doc_id=1, url=_SELF_POST_URL)

        assert result is not None
        assert result.status == "fetched"
        assert result.title == "A deep dive into transformers"
        assert "attention mechanisms" in (result.text or "")
        assert result.error_msg == "fetched via reddit json"

    @pytest.mark.asyncio
    async def test_fetches_link_post_with_comments(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = httpx.Response(
            200, json=_LINK_POST_JSON, request=httpx.Request("GET", "http://x")
        )

        result = await fetch_reddit_thread(mock_client, doc_id=1, url=_LINK_POST_URL)

        assert result is not None
        assert result.status == "fetched"
        assert "Great read!" in (result.text or "")
        assert "AutoModerator" not in (result.text or "")
        assert result.card_summary is None

    @pytest.mark.asyncio
    async def test_blocked_falls_back_to_url_derived_title(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = httpx.Response(403, request=httpx.Request("GET", "http://x"))

        result = await fetch_reddit_thread(mock_client, doc_id=1, url=_SELF_POST_URL)

        assert result is not None
        assert result.status == "fetched"
        assert result.title == "A deep dive into transformers"
        assert "r/MachineLearning" in (result.text or "")
        assert "fallback" in (result.error_msg or "")

    @pytest.mark.asyncio
    async def test_blocked_with_no_slug_is_unfetchable(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = httpx.Response(403, request=httpx.Request("GET", "http://x"))

        url = "https://www.reddit.com/r/programming/comments/def456/"
        result = await fetch_reddit_thread(mock_client, doc_id=1, url=url)

        assert result is not None
        assert result.status == "unfetchable"

    @pytest.mark.asyncio
    async def test_blocked_redd_it_is_unfetchable(self):
        """No subreddit is ever encoded in a redd.it short link, so no fallback is possible."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = httpx.Response(403, request=httpx.Request("GET", "http://x"))

        result = await fetch_reddit_thread(mock_client, doc_id=1, url="https://redd.it/abc123")

        assert result is not None
        assert result.status == "unfetchable"

    @pytest.mark.asyncio
    async def test_non_reddit_returns_none(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        assert await fetch_reddit_thread(mock_client, 1, "https://example.com") is None
