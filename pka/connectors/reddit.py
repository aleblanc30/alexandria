"""
Reddit saved-posts connector (live OAuth API via PRAW).

Unlike the Firefox/Zotero/Calibre connectors, Reddit saved items live behind an
authenticated web API rather than a local file. Credentials come from
``ALEXANDRIA_REDDIT_*`` env / ``.env`` only and are never committed.

``praw`` is an **optional** dependency (``pip install -e '.[reddit]'``); it is
imported lazily so the rest of Alexandria — and the test suite — runs without it.
Both saved submissions (``t3_``) and saved comments (``t1_``) are returned.

Content handling:
  - self-posts / comments carry their body inline (``body``); the runner embeds
    it in phase 1 (no HTTP fetch owed).
  - link posts expose ``external_url``; the runner queues those for the phase-2
    fetcher (reusing the Firefox fetch machinery).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pka.config import settings as cfg

log = logging.getLogger(__name__)

_DELETED_BODIES = {"[deleted]", "[removed]"}


class RedditConnectorError(RuntimeError):
    """Raised when the Reddit connector cannot load saved posts."""


@dataclass
class RedditSaved:
    source_id: str            # reddit fullname, e.g. "t3_abc123" / "t1_def456"
    kind: str                 # "post" | "comment"
    title: str
    permalink: str            # canonical reddit thread URL (always present)
    external_url: str | None  # off-reddit target for link posts, else None
    subreddit: str            # display name, e.g. "MachineLearning"
    body: str | None          # selftext / comment body (may be None)
    date_added: int | None    # unix seconds (item creation; Reddit exposes no "saved at")
    tags: list[str] = field(default_factory=list)  # checklist-required; unused today

    @property
    def url_or_path(self) -> str:
        """Document URL: the external target for link posts, else the permalink."""
        return self.external_url or self.permalink

    @property
    def collection(self) -> str:
        """Subreddit rendered as a folder-like collection path."""
        return f"r/{self.subreddit}" if self.subreddit else "r/"


# ── PRAW client ──────────────────────────────────────────────────────────────

def _build_client():
    """Construct a read-only PRAW client from configured credentials."""
    try:
        import praw
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise RedditConnectorError(
            "The Reddit connector requires praw. Install it with: "
            "pip install -e '.[reddit]'"
        ) from exc

    if not cfg.reddit_client_id or not cfg.reddit_client_secret:
        raise RedditConnectorError(
            "Reddit credentials missing. Set ALEXANDRIA_REDDIT_CLIENT_ID and "
            "ALEXANDRIA_REDDIT_CLIENT_SECRET (create a 'script' app at "
            "https://www.reddit.com/prefs/apps)."
        )

    kwargs = dict(
        client_id=cfg.reddit_client_id,
        client_secret=cfg.reddit_client_secret,
        user_agent=cfg.reddit_user_agent,
    )
    if cfg.reddit_refresh_token:
        kwargs["refresh_token"] = cfg.reddit_refresh_token
    elif cfg.reddit_username and cfg.reddit_password:
        kwargs["username"] = cfg.reddit_username
        kwargs["password"] = cfg.reddit_password
    else:
        raise RedditConnectorError(
            "Reddit auth missing. Set ALEXANDRIA_REDDIT_REFRESH_TOKEN, or both "
            "ALEXANDRIA_REDDIT_USERNAME and ALEXANDRIA_REDDIT_PASSWORD."
        )

    reddit = praw.Reddit(**kwargs)
    reddit.read_only = True
    return reddit


# ── Item mapping ─────────────────────────────────────────────────────────────

def _abs_permalink(permalink: object) -> str:
    p = str(permalink or "").strip()
    if not p:
        return ""
    if p.startswith("http://") or p.startswith("https://"):
        return p
    if not p.startswith("/"):
        p = "/" + p
    return "https://www.reddit.com" + p


def _clean_body(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or text in _DELETED_BODIES:
        return None
    return text


def _detect_kind(item: object) -> str:
    fullname = str(getattr(item, "name", "") or "")
    if fullname.startswith("t1_"):
        return "comment"
    if fullname.startswith("t3_"):
        return "post"
    # Fallback for objects without a fullname: submissions carry a title.
    return "post" if getattr(item, "title", None) else "comment"


def _source_id(item: object, kind: str) -> str:
    fullname = str(getattr(item, "name", "") or "")
    if fullname:
        return fullname
    prefix = "t1" if kind == "comment" else "t3"
    return f"{prefix}_{getattr(item, 'id', '') or ''}"


def _to_saved(item: object) -> RedditSaved:
    kind = _detect_kind(item)
    subreddit = str(getattr(item, "subreddit", "") or "").strip()
    permalink = _abs_permalink(getattr(item, "permalink", ""))
    created = getattr(item, "created_utc", None)
    date_added = int(created) if created else None

    if kind == "comment":
        link_title = str(getattr(item, "link_title", "") or "").strip()
        title = f'Comment on "{link_title}"' if link_title else (
            f"Comment in r/{subreddit}" if subreddit else "Reddit comment"
        )
        body = _clean_body(getattr(item, "body", None))
        external_url = None
    else:
        title = str(getattr(item, "title", "") or "").strip() or "Reddit post"
        body = _clean_body(getattr(item, "selftext", None))
        is_self = bool(getattr(item, "is_self", False))
        raw_url = str(getattr(item, "url", "") or "").strip()
        # A self-post's url is just its own permalink → no external target.
        external_url = None if (is_self or not raw_url) else raw_url

    return RedditSaved(
        source_id=_source_id(item, kind),
        kind=kind,
        title=title,
        permalink=permalink,
        external_url=external_url,
        subreddit=subreddit,
        body=body,
        date_added=date_added,
    )


# ── Main loader ──────────────────────────────────────────────────────────────

def load_saved(limit: int | None = -1, client: object | None = None) -> list[RedditSaved]:
    """Load the authenticated user's saved posts and comments.

    ``limit`` defaults to :data:`settings.reddit_saved_limit` (``None`` = all).
    Pass ``client`` to inject a pre-built PRAW-compatible client (used by tests
    so ``praw`` need not be installed).
    """
    if limit == -1:
        limit = cfg.reddit_saved_limit

    reddit = client if client is not None else _build_client()

    me = reddit.user.me()
    if me is None:
        raise RedditConnectorError(
            "Reddit authentication succeeded but no user is associated with the "
            "token (read-only app credentials cannot list saved posts)."
        )

    saved: list[RedditSaved] = []
    seen: set[str] = set()
    for item in me.saved(limit=limit):
        entry = _to_saved(item)
        if not entry.source_id or entry.source_id in seen:
            continue
        seen.add(entry.source_id)
        saved.append(entry)

    log.info("Loaded %d Reddit saved items", len(saved))
    return saved
