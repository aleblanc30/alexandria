"""Reddit ``.json`` fetch for Firefox bookmark URLs pointing at a thread.

Named ``reddit_bookmark`` rather than ``reddit`` to stay clearly separate from
``pka/connectors/reddit.py`` / ``ingestion/reddit_sync.py`` / ``runners/reddit.py``
— the unrelated Atom-feed connector that pulls the user's own saved posts. This
module only recognizes an arbitrary bookmarked ``reddit.com``/``redd.it`` thread
link and enriches it via Reddit's public, unauthenticated ``.json`` listing
(title + selftext, or the top comments for a link post with no selftext).

Reddit has tightened anonymous access since 2023; a 403/429/timeout on the
``.json`` call falls back to a **URL-only** guess — no second HTTP call —
derived from the auto-generated slug Reddit embeds in the permalink itself
(``/r/<sub>/comments/<id>/<slug>/``). This is lossy (lowercased, truncated,
punctuation stripped) but keeps the bookmark searchable by subreddit + a rough
title instead of going fully ``unfetchable`` the moment Reddit blocks the
request. The fallback only fires when both a subreddit and a slug are present
in the URL; a bare ``redd.it`` short link or a slug-less permalink carries too
little to guess from and falls through to ``unfetchable`` as before.

See ``planning/FIREFOX_INGESTERS_PLAN.md`` §3 for the full design.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from pka.card_summary import body_excerpt
from pka.ingestion.fetch_base import FetchResult, _http_timeout, _limiter

_REDDIT_HOST = re.compile(r"^(?:www\.|old\.|np\.)?reddit\.com$", re.IGNORECASE)
_REDD_IT_HOST = re.compile(r"^(?:www\.)?redd\.it$", re.IGNORECASE)
_PERMALINK_PATH = re.compile(
    r"^/r/(?P<subreddit>[A-Za-z0-9_]+)/comments/(?P<post_id>[a-z0-9]+)(?:/(?P<slug>[^/?#]*))?/?$",
    re.IGNORECASE,
)
_REDD_IT_PATH = re.compile(r"^/(?P<post_id>[a-z0-9]+)/?$", re.IGNORECASE)

_DELETED_BODIES = {"[deleted]", "[removed]"}
_MAX_FALLBACK_COMMENTS = 5


@dataclass(frozen=True)
class RedditPermalink:
    subreddit: str | None  # None for a redd.it short link — not encoded in the URL
    post_id: str
    slug: str | None  # raw slug segment, when the URL carries one


@dataclass(frozen=True)
class RedditThread:
    title: str
    subreddit: str
    is_self: bool
    selftext: str  # empty for link posts
    external_url: str | None  # link-post target; None for self posts
    comments: list[str]  # top comment bodies, deleted/AutoModerator filtered


def is_reddit_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(_REDDIT_HOST.match(host) or _REDD_IT_HOST.match(host))


def parse_reddit_permalink(url: str) -> RedditPermalink | None:
    """Return the ``(subreddit, post_id, slug)`` a thread bookmark encodes, or ``None``."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if _REDDIT_HOST.match(host):
        match = _PERMALINK_PATH.match(parsed.path)
        if not match:
            return None
        return RedditPermalink(
            subreddit=match.group("subreddit"),
            post_id=match.group("post_id"),
            slug=match.group("slug") or None,
        )
    if _REDD_IT_HOST.match(host):
        match = _REDD_IT_PATH.match(parsed.path)
        if not match:
            return None
        return RedditPermalink(subreddit=None, post_id=match.group("post_id"), slug=None)
    return None


def reddit_json_url(subreddit: str | None, post_id: str) -> str:
    if subreddit:
        return f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/.json"
    return f"https://www.reddit.com/comments/{post_id}/.json"


def _slug_to_title_guess(slug: str) -> str:
    """Best-effort human title from Reddit's auto-generated permalink slug."""
    guess = " ".join(slug.replace("-", " ").replace("_", " ").split())
    return guess[:1].upper() + guess[1:] if guess else ""


def parse_reddit_listing(data: object) -> RedditThread | None:
    """Parse a Reddit ``.json`` thread response: ``[post Listing, comments Listing]``."""
    if not isinstance(data, list) or not data:
        return None
    try:
        post = data[0]["data"]["children"][0]["data"]
    except (KeyError, IndexError, TypeError):
        return None

    title = (post.get("title") or "").strip()
    subreddit = (post.get("subreddit") or "").strip()
    if not title or not subreddit:
        return None

    is_self = bool(post.get("is_self"))
    selftext = (post.get("selftext") or "").strip()
    external_url = None if is_self else (post.get("url") or None)

    comments: list[str] = []
    if len(data) > 1:
        try:
            children = data[1]["data"]["children"]
        except (KeyError, TypeError):
            children = []
        for child in children:
            if not isinstance(child, dict) or child.get("kind") != "t1":
                continue
            c = child.get("data") or {}
            author = (c.get("author") or "").strip()
            body = (c.get("body") or "").strip()
            if not body or body in _DELETED_BODIES or author == "AutoModerator":
                continue
            comments.append(body)
            if len(comments) >= _MAX_FALLBACK_COMMENTS:
                break

    return RedditThread(
        title=title,
        subreddit=subreddit,
        is_self=is_self,
        selftext=selftext,
        external_url=external_url,
        comments=comments,
    )


def _embed_text(thread: RedditThread) -> str:
    parts = [thread.title]
    if thread.selftext:
        parts.append(thread.selftext)
    elif thread.comments:
        parts.append("\n".join(f"— {c}" for c in thread.comments))
    return "\n\n".join(parts)


async def _fetch_reddit_metadata(
    client: httpx.AsyncClient,
    permalink: RedditPermalink,
) -> tuple[RedditThread | None, int | None, str | None]:
    api_url = reddit_json_url(permalink.subreddit, permalink.post_id)
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

    thread = parse_reddit_listing(data)
    if thread is None:
        return None, resp.status_code, "reddit json returned no post"
    return thread, resp.status_code, None


def _url_fallback_result(
    doc_id: int,
    url: str,
    permalink: RedditPermalink,
    http_status: int | None,
    err: str | None,
) -> FetchResult | None:
    """URL-only guess when the ``.json`` call fails — see module docstring."""
    if not permalink.subreddit or not permalink.slug:
        return None
    title = _slug_to_title_guess(permalink.slug)
    if not title:
        return None
    text = f"r/{permalink.subreddit}\n\n{title}"
    return FetchResult(
        doc_id,
        url,
        "fetched",
        text,
        http_status,
        f"reddit json unavailable ({err}); used url-derived title/subreddit fallback",
        title=title,
    )


async def fetch_reddit_thread(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
) -> FetchResult | None:
    """Fetch a Reddit thread bookmark via its ``.json`` listing.

    Returns ``None`` when ``url`` is not a Reddit thread URL (dispatch in
    ``pka/ingestion/fetcher.py`` falls through to the next handler).
    """
    permalink = parse_reddit_permalink(url)
    if permalink is None:
        return None

    thread, http_status, err = await _fetch_reddit_metadata(client, permalink)
    if thread is None:
        fallback = _url_fallback_result(doc_id, url, permalink, http_status, err)
        if fallback is not None:
            return fallback
        return FetchResult(doc_id, url, "unfetchable", None, http_status, err)

    card_summary = body_excerpt(thread.selftext) if thread.selftext else None

    return FetchResult(
        doc_id,
        url,
        "fetched",
        _embed_text(thread),
        http_status,
        "fetched via reddit json",
        title=thread.title,
        card_summary=card_summary,
    )
