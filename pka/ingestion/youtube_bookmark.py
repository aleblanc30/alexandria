"""YouTube oEmbed fetch for Firefox bookmark URLs.

Named ``youtube_bookmark`` rather than ``youtube`` to stay clearly separate
from ``pka/connectors/youtube.py`` / ``ingestion/youtube_sync.py`` /
``runners/youtube.py`` — the unrelated Data-API connector that pulls the
user's own saved/liked videos. This module only recognizes an arbitrary
bookmarked ``youtube.com``/``youtu.be`` link and enriches it via the public,
unauthenticated oEmbed endpoint (title + channel name, no API key, no quota).

A bookmark that is not a *video* — a channel, a handle, a playlist — has no
video id to key oEmbed off, and scraping it returns Google's consent
interstitial rather than page content ("Bevor Sie zu YouTube weitergehen…"),
which then becomes the card. ``youtube_page_result`` builds a card from the URL
instead, with no request at all — the same shape as ``search_url.py`` and
``researchgate.py``.
Deliberately decoupled from the Data-API connector's credentials — per
``DESIGN.md`` §1.1, enabling one outbound path must never be a prerequisite
for another. See ``planning/FIREFOX_INGESTERS_PLAN.md`` §2 for the full design.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

from pka.card_summary import SUMMARY_MAX_LEN, truncate_summary
from pka.ingestion.fetch_base import FetchResult, _http_timeout, _limiter

_YOUTUBE_HOST = re.compile(r"^(?:www\.|m\.)?youtube\.com$", re.IGNORECASE)
_YOUTU_BE_HOST = re.compile(r"^(?:www\.)?youtu\.be$", re.IGNORECASE)
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_SHORTS_PATH = re.compile(r"^/shorts/([A-Za-z0-9_-]{11})/?$")
_YOUTU_BE_PATH = re.compile(r"^/([A-Za-z0-9_-]{11})/?$")

# Channel URL shapes, all of which carry a readable name except /channel/UC…,
# which carries an opaque id and is left to the id itself.
_HANDLE_PATH = re.compile(r"^/@([^/]+)")
_NAMED_CHANNEL_PATH = re.compile(r"^/(?:c|user)/([^/]+)")
_CHANNEL_ID_PATH = re.compile(r"^/channel/([^/]+)")
# Tabs a channel URL may end on; not part of the name.
_CHANNEL_TABS = ("/videos", "/featured", "/streams", "/shorts", "/playlists", "/community")
# Room for the "YouTube channel: " prefix and the trailing period.
_TITLE_MAX_LEN = SUMMARY_MAX_LEN - 30


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


@dataclass(frozen=True)
class YoutubePage:
    """A non-video YouTube bookmark: ``kind`` is "channel" or "playlist"."""

    kind: str
    name: str


def parse_youtube_page_url(url: str) -> YoutubePage | None:
    """Return the channel or playlist a non-video YouTube URL names, or ``None``.

    ``None`` for a video URL (``parse_youtube_url``'s job), for
    ``/results?search_query=`` (``search_url.py``'s, and it must stay that way
    so ``search_url_cards`` keeps meaning something), and for any other host.
    """
    parsed = urlparse(url)
    if not _YOUTUBE_HOST.match((parsed.hostname or "").lower()):
        return None
    if parse_youtube_url(url) is not None:
        return None
    path = parsed.path.rstrip("/") or "/"
    if path == "/results":
        return None
    for tab in _CHANNEL_TABS:
        if path.endswith(tab):
            path = path[: -len(tab)] or "/"
            break

    if path in ("/playlist", "/watch_videos"):
        list_id = (parse_qs(parsed.query).get("list") or [""])[0].strip()
        return YoutubePage("playlist", list_id) if list_id else None

    for pattern in (_HANDLE_PATH, _NAMED_CHANNEL_PATH, _CHANNEL_ID_PATH):
        match = pattern.match(path)
        if match:
            return YoutubePage("channel", unquote(match.group(1)))
    return None


def youtube_page_result(doc_id: int, url: str) -> FetchResult | None:
    """Build a card from a channel or playlist URL — no HTTP request.

    Returns ``None`` when ``url`` is not one (dispatch in
    ``pka/ingestion/fetcher.py`` falls through to the next handler).
    """
    page = parse_youtube_page_url(url)
    if page is None:
        return None

    name = truncate_summary(page.name.replace("_", " "), _TITLE_MAX_LEN)
    if not name:
        return None
    title = f"{name} — YouTube {page.kind}"

    return FetchResult(
        doc_id,
        url,
        "fetched",
        f"{name}\n\nYouTube {page.kind}",
        None,
        f"youtube {page.kind}; card built from url, no fetch",
        title=title,
        # Set explicitly so embed_fetched_text does not fall back to
        # body_excerpt() over the two-line text above.
        card_summary=f"YouTube {page.kind}: {name}.",
    )


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
