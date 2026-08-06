"""YouTube Data API v3 connector for saved videos.

Reads the authenticated user's playlists (including the special *Liked videos*
playlist) and the videos inside each, returning one :class:`YouTubeVideo` per
unique video. **Metadata only** — title, channel, description, and the video's
own tags. Transcript extraction is deferred (see ``BACKLOG.md``).

Cloud exception: Alexandria is otherwise strictly local-first. This is the one
sanctioned outbound integration and is **inert** unless YouTube OAuth
credentials are configured (``ALEXANDRIA_YOUTUBE_CLIENT_SECRET``). Scope is
read-only (``youtube.readonly``); the OAuth refresh token is cached locally at
``data/youtube_token.json`` and never committed.

The heavy ``google-api-python-client`` / ``google-auth-oauthlib`` dependencies
are lazy-imported inside the auth helpers so this module stays importable (and
testable via an injected fake ``service``) without the optional ``youtube``
extra installed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from pka.config import settings as cfg

log = logging.getLogger(__name__)

YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
_API_SERVICE_NAME = "youtube"
_API_VERSION = "v3"
_PAGE_SIZE = 50               # max maxResults for playlists / playlistItems
_VIDEO_HYDRATE_BATCH = 50     # videos.list accepts up to 50 ids per call
LIKED_PLAYLIST_TITLE = "Liked videos"


class YouTubeAuthError(RuntimeError):
    """Raised when YouTube credentials are missing, invalid, or unavailable."""


@dataclass
class YouTubeVideo:
    source_id: str            # YouTube video id (stable within the source)
    url: str                  # canonical watch URL
    title: str
    channel: str              # channel / video owner title
    description: str
    tags: list[str] = field(default_factory=list)       # the video's own tags
    playlists: list[str] = field(default_factory=list)  # playlist titles containing it
    date_added: int | None = None  # earliest playlist-add time (unix seconds)


# ── Small pure helpers (import-safe, unit-tested) ─────────────────────────────

def video_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def parse_timestamp(value: str | None) -> int | None:
    """RFC3339 timestamp (``…Z``) → unix seconds, or ``None`` when unparseable."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def youtube_embed_text(video: YouTubeVideo) -> str:
    """Assemble the embeddable text block for a saved video (metadata only)."""
    parts = [video.title]
    if video.channel:
        parts.append(f"Channel: {video.channel}")
    if video.description:
        parts.append(video.description)
    if video.tags:
        parts.append("Tags: " + ", ".join(video.tags))
    return "\n\n".join(p for p in parts if p and p.strip())


def youtube_card_summary(video: YouTubeVideo) -> str | None:
    """Browse-card excerpt: the video description, or ``None`` when empty."""
    raw = (video.description or "").strip()
    return raw or None


def youtube_credentials_available() -> tuple[bool, str | None]:
    """Cheap, network-free check for whether the connector can run.

    True once either a cached token or the OAuth client secret exists. Used by
    status polling so we never touch the network just to render availability.
    """
    if cfg.youtube_token_path.exists() or cfg.youtube_client_secret.exists():
        return True, None
    return False, (
        "YouTube not configured. Create a desktop-app OAuth client in Google "
        "Cloud Console and set ALEXANDRIA_YOUTUBE_CLIENT_SECRET to the "
        f"downloaded JSON (looked at {cfg.youtube_client_secret})."
    )


# ── Auth + client construction (lazy google imports) ──────────────────────────

def _require_google_libs() -> None:
    try:
        import google_auth_oauthlib  # noqa: F401
        import googleapiclient  # noqa: F401
    except ImportError as exc:
        raise YouTubeAuthError(
            "YouTube support needs optional dependencies. Install them with: "
            "pip install -e '.[youtube]'"
        ) from exc


def _load_credentials():
    """Build read-only OAuth credentials from the client secret + cached token.

    Refreshes an expired token silently; otherwise runs the one-time installed
    -app consent flow (opens a browser on the local machine) and caches the
    resulting refresh token under ``data/``.
    """
    _require_google_libs()
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token = cfg.youtube_token_path
    secret = cfg.youtube_client_secret
    scopes = [YOUTUBE_READONLY_SCOPE]

    creds = None
    if token.exists():
        creds = Credentials.from_authorized_user_file(str(token), scopes)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not secret.exists():
            raise YouTubeAuthError(
                f"YouTube client secret not found at {secret}. Create a "
                "desktop-app OAuth client in Google Cloud Console and set "
                "ALEXANDRIA_YOUTUBE_CLIENT_SECRET to the downloaded JSON."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(secret), scopes)
        creds = flow.run_local_server(port=0)

    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_service():
    """Construct an authenticated YouTube Data API client."""
    _require_google_libs()
    from googleapiclient.discovery import build

    return build(
        _API_SERVICE_NAME,
        _API_VERSION,
        credentials=_load_credentials(),
        cache_discovery=False,
    )


# ── API traversal (operate on a duck-typed ``service`` → fake-able in tests) ───

def _liked_playlist(service) -> tuple[str, str] | None:
    """Resolve the user's *Liked videos* playlist id (not returned by mine=True)."""
    resp = service.channels().list(part="contentDetails", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        return None
    related = (items[0].get("contentDetails") or {}).get("relatedPlaylists") or {}
    likes = related.get("likes")
    return (likes, LIKED_PLAYLIST_TITLE) if likes else None


def _list_owned_playlists(service) -> list[tuple[str, str]]:
    """Return ``(playlist_id, title)`` for every playlist the user owns."""
    out: list[tuple[str, str]] = []
    page_token = None
    while True:
        resp = service.playlists().list(
            part="snippet", mine=True, maxResults=_PAGE_SIZE, pageToken=page_token,
        ).execute()
        for item in resp.get("items", []):
            pid = item.get("id")
            title = (item.get("snippet") or {}).get("title", "")
            if pid:
                out.append((pid, title))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def _list_playlist_items(service, playlist_id: str) -> list[tuple[str, int | None]]:
    """Return ``(video_id, added_at)`` for every item in a playlist.

    ``added_at`` is ``snippet.publishedAt`` — when the video was added to the
    playlist (i.e. when the user saved it), not when the video was published.
    """
    out: list[tuple[str, int | None]] = []
    page_token = None
    while True:
        resp = service.playlistItems().list(
            part="snippet,contentDetails",
            playlistId=playlist_id,
            maxResults=_PAGE_SIZE,
            pageToken=page_token,
        ).execute()
        for item in resp.get("items", []):
            snippet = item.get("snippet") or {}
            content = item.get("contentDetails") or {}
            video_id = content.get("videoId") or (snippet.get("resourceId") or {}).get("videoId")
            if video_id:
                out.append((video_id, parse_timestamp(snippet.get("publishedAt"))))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def _hydrate_videos(service, video_ids: list[str]) -> dict[str, dict]:
    """Batch-fetch canonical ``snippet`` blocks keyed by video id."""
    details: dict[str, dict] = {}
    for start in range(0, len(video_ids), _VIDEO_HYDRATE_BATCH):
        batch = video_ids[start:start + _VIDEO_HYDRATE_BATCH]
        resp = service.videos().list(part="snippet", id=",".join(batch)).execute()
        for item in resp.get("items", []):
            vid = item.get("id")
            if vid:
                details[vid] = item.get("snippet") or {}
    return details


def load_saved_videos(service=None) -> list[YouTubeVideo]:
    """Load every saved video across the user's playlists (Liked + owned).

    A video appearing in multiple playlists collapses to one document whose
    ``playlists`` lists each containing playlist and whose ``date_added`` is the
    earliest save time. Pass a fake ``service`` in tests to avoid the network.
    """
    service = service or build_service()

    playlists: list[tuple[str, str]] = []
    if (liked := _liked_playlist(service)) is not None:
        playlists.append(liked)
    playlists.extend(_list_owned_playlists(service))

    # video_id -> {"playlists": [titles], "added": earliest unix ts | None}
    membership: dict[str, dict] = {}
    order: list[str] = []
    for playlist_id, title in playlists:
        for video_id, added_at in _list_playlist_items(service, playlist_id):
            entry = membership.get(video_id)
            if entry is None:
                entry = {"playlists": [], "added": added_at}
                membership[video_id] = entry
                order.append(video_id)
            if title and title not in entry["playlists"]:
                entry["playlists"].append(title)
            if added_at is not None and (entry["added"] is None or added_at < entry["added"]):
                entry["added"] = added_at

    details = _hydrate_videos(service, order)

    videos: list[YouTubeVideo] = []
    for video_id in order:
        entry = membership[video_id]
        snippet = details.get(video_id, {})
        title = (snippet.get("title") or "").strip() or video_id
        videos.append(YouTubeVideo(
            source_id=video_id,
            url=video_watch_url(video_id),
            title=title,
            channel=(snippet.get("channelTitle") or "").strip(),
            description=(snippet.get("description") or "").strip(),
            tags=list(snippet.get("tags") or []),
            playlists=entry["playlists"],
            date_added=entry["added"],
        ))

    log.info(
        "Loaded %d saved YouTube videos across %d playlists",
        len(videos), len(playlists),
    )
    return videos
