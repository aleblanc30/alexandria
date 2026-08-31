"""Tests for YouTube bookmark URL parsing, oEmbed parsing, and fetch handler."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from pka.ingestion.youtube_bookmark import (
    YoutubeVideo,
    fetch_youtube_video,
    is_youtube_url,
    parse_youtube_oembed,
    parse_youtube_url,
)

_VIDEO_ID = "dQw4w9WgXcQ"

_OEMBED_JSON = {
    "title": "Never Gonna Give You Up",
    "author_name": "Rick Astley",
    "provider_name": "YouTube",
}


class TestParseYoutubeUrl:
    def test_watch_url(self):
        assert parse_youtube_url(f"https://www.youtube.com/watch?v={_VIDEO_ID}") == _VIDEO_ID

    def test_watch_url_no_www(self):
        assert parse_youtube_url(f"https://youtube.com/watch?v={_VIDEO_ID}") == _VIDEO_ID

    def test_watch_url_mobile_host(self):
        assert parse_youtube_url(f"https://m.youtube.com/watch?v={_VIDEO_ID}") == _VIDEO_ID

    def test_watch_url_with_extra_query_params(self):
        url = f"https://www.youtube.com/watch?v={_VIDEO_ID}&t=42s&list=PL123"
        assert parse_youtube_url(url) == _VIDEO_ID

    def test_shorts_url(self):
        assert parse_youtube_url(f"https://www.youtube.com/shorts/{_VIDEO_ID}") == _VIDEO_ID

    def test_youtu_be_short_link(self):
        assert parse_youtube_url(f"https://youtu.be/{_VIDEO_ID}") == _VIDEO_ID

    def test_youtu_be_short_link_with_query(self):
        assert parse_youtube_url(f"https://youtu.be/{_VIDEO_ID}?si=abc123") == _VIDEO_ID

    def test_playlist_only_url_returns_none(self):
        assert parse_youtube_url("https://www.youtube.com/playlist?list=PL123") is None

    def test_channel_url_returns_none(self):
        assert parse_youtube_url("https://www.youtube.com/channel/UC12345") is None

    def test_non_youtube_returns_none(self):
        assert parse_youtube_url("https://example.com/watch?v=abc") is None

    def test_watch_url_missing_v_param_returns_none(self):
        assert parse_youtube_url("https://www.youtube.com/watch?list=PL123") is None

    def test_is_youtube_url(self):
        assert is_youtube_url("https://www.youtube.com/")
        assert is_youtube_url("https://youtu.be/")
        assert not is_youtube_url("https://example.com/")


class TestParseYoutubeOembed:
    def test_parses_title_and_author(self):
        video = parse_youtube_oembed(_OEMBED_JSON)
        assert video == YoutubeVideo(title="Never Gonna Give You Up", author_name="Rick Astley")

    def test_missing_title_returns_none(self):
        assert parse_youtube_oembed({"author_name": "Rick Astley"}) is None

    def test_missing_author_defaults_to_empty(self):
        video = parse_youtube_oembed({"title": "Some Video"})
        assert video is not None
        assert video.author_name == ""


class TestFetchYoutubeVideo:
    @pytest.mark.asyncio
    async def test_fetches_metadata(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = httpx.Response(
            200,
            json=_OEMBED_JSON,
            request=httpx.Request("GET", "http://x"),
        )

        result = await fetch_youtube_video(
            mock_client,
            doc_id=1,
            url=f"https://www.youtube.com/watch?v={_VIDEO_ID}",
        )

        assert result is not None
        assert result.status == "fetched"
        assert result.title == "Never Gonna Give You Up"
        assert result.text == "Never Gonna Give You Up\n\nby Rick Astley"
        assert result.error_msg == "fetched via youtube oembed"

    @pytest.mark.asyncio
    async def test_fetches_metadata_without_author(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = httpx.Response(
            200,
            json={"title": "Some Video"},
            request=httpx.Request("GET", "http://x"),
        )

        result = await fetch_youtube_video(
            mock_client,
            doc_id=1,
            url=f"https://www.youtube.com/watch?v={_VIDEO_ID}",
        )

        assert result is not None
        assert result.text == "Some Video"

    @pytest.mark.asyncio
    async def test_private_or_deleted_video_unfetchable(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = httpx.Response(
            404,
            request=httpx.Request("GET", "http://x"),
        )

        result = await fetch_youtube_video(
            mock_client,
            doc_id=1,
            url=f"https://www.youtube.com/watch?v={_VIDEO_ID}",
        )

        assert result is not None
        assert result.status == "unfetchable"
        assert result.http_status == 404

    @pytest.mark.asyncio
    async def test_non_youtube_returns_none(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        assert await fetch_youtube_video(mock_client, 1, "https://example.com") is None
