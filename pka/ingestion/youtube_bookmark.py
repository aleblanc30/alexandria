"""YouTube oEmbed fetch for Firefox bookmark URLs.

Named ``youtube_bookmark`` rather than ``youtube`` to stay clearly separate
from ``pka/connectors/youtube.py`` / ``ingestion/youtube_sync.py`` /
``runners/youtube.py`` — the unrelated Data-API connector that pulls the
user's own saved/liked videos. This module only recognizes an arbitrary
bookmarked ``youtube.com``/``youtu.be`` link and enriches it via the public,
unauthenticated oEmbed endpoint (title + channel name, no API key, no quota).
Deliberately decoupled from the Data-API connector's credentials — per
``DESIGN.md`` §1.1, enabling one outbound path must never be a prerequisite
for another. See ``planning/FIREFOX_INGESTERS_PLAN.md`` §2 for the full design.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlparse

import httpx

from pka.ingestion.fetch_base import FetchResult, _http_timeout, _limiter

_YOUTUBE_HOST = re.compile(r"^(?:www\.|m\.)?youtube\.com$", re.IGNORECASE)
_YOUTU_BE_HOST = re.compile(r"^(?:www\.)?youtu\.be$", re.IGNORECASE)
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_SHORTS_PATH = re.compile(r"^/shorts/([A-Za-z0-9_-]{11})/?$")
_YOUTU_BE_PATH = re.compile(r"^/([A-Za-z0-9_-]{11})/?$")


@dataclass(frozen=True)
class YoutubeVideo:
    title: str
    author_name: str  # channel name; empty string when oEmbed omits it


def is_youtube_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(_YOUTUBE_HOST.match(host) or _YOUTU_BE_HOST.match(host))


def parse_youtube_url(url: str) -> str | None:
    """Return the video ID for a fetchable YouTube video URL, or ``None``.

    Matches ``(www.|m.)?youtube.com/watch?v=ID``, ``youtube.com/shorts/ID``,
    and ``youtu.be/ID``. Playlist-only and channel URLs have no single video ID
    to key off and so return ``None``.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if _YOUTUBE_HOST.match(host):
        if parsed.path == "/watch":
            video_id = (parse_qs(parsed.query).get("v") or [None])[0]
        else:
            match = _SHORTS_PATH.match(parsed.path)
            video_id = match.group(1) if match else None
    elif _YOUTU_BE_HOST.match(host):
        match = _YOUTU_BE_PATH.match(parsed.path)
        video_id = match.group(1) if match else None
    else:
        return None

    if video_id and _VIDEO_ID_RE.match(video_id):
        return video_id
    return None


def youtube_oembed_url(video_id: str) -> str:
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    return f"https://www.youtube.com/oembed?url={quote(watch_url, safe='')}&format=json"


def parse_youtube_oembed(data: dict) -> YoutubeVideo | None:
    """Extract title + channel from an oEmbed JSON response. Requires a title."""
    title = (data.get("title") or "").strip()
    if not title:
        return None
    author_name = (data.get("author_name") or "").strip()
    return YoutubeVideo(title=title, author_name=author_name)


async def _fetch_youtube_metadata(
    client: httpx.AsyncClient,
    video_id: str,
) -> tuple[YoutubeVideo | None, int | None, str | None]:
    api_url = youtube_oembed_url(video_id)
    await _limiter.wait(api_url)
    try:
        resp = await client.get(
            api_url,
            follow_redirects=True,
            timeout=_http_timeout(),
        )
    except httpx.TimeoutException:
        return None, None, "timeout"
    except httpx.RequestError as exc:
        return None, None, str(exc)

    if resp.status_code >= 400:
        return None, resp.status_code, f"HTTP {resp.status_code}"

    try:
        data = resp.json()
    except ValueError:
        return None, resp.status_code, "invalid json response"

    video = parse_youtube_oembed(data)
    if video is None:
        return None, resp.status_code, "oembed response missing title"
    return video, resp.status_code, None


async def fetch_youtube_video(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
) -> FetchResult | None:
    """Fetch YouTube title + channel for a bookmark URL via oEmbed.

    Returns ``None`` when ``url`` is not a YouTube video URL (dispatch in
    ``pka/ingestion/fetcher.py`` falls through to the next handler).
    """
    video_id = parse_youtube_url(url)
    if not video_id:
        return None

    video, http_status, err = await _fetch_youtube_metadata(client, video_id)
    if video is None:
        return FetchResult(doc_id, url, "unfetchable", None, http_status, err)

    text = f"{video.title}\n\nby {video.author_name}" if video.author_name else video.title

    return FetchResult(
        doc_id,
        url,
        "fetched",
        text,
        http_status,
        "fetched via youtube oembed",
        title=video.title,
    )
