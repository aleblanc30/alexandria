"""Wikipedia MediaWiki API fetch for Firefox bookmark URLs."""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import sqlalchemy as sa

from pka.config import settings as cfg
from pka.ingestion.fetcher import FetchResult, _http_timeout, _limiter

log = logging.getLogger(__name__)

_WIKI_HOST = re.compile(r"^([a-z][\w-]*)\.(?:m\.)?wikipedia\.org$", re.IGNORECASE)
_API_ENDPOINT = "/w/api.php"


def _normalize_title(title: str) -> str:
    title = title.strip().replace(" ", "_")
    return title.rstrip("/")


def parse_wikipedia_url(url: str) -> tuple[str, str] | None:
    """Return ``(lang, title)`` for a fetchable Wikipedia article URL, or ``None``."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    match = _WIKI_HOST.match(host)
    if not match:
        return None

    lang = match.group(1)
    path = parsed.path

    if path.startswith("/wiki/"):
        title = unquote(path[len("/wiki/") :].split("#", 1)[0])
    elif path.rstrip("/") == "/w/index.php" or path.endswith("/w/index.php"):
        titles = parse_qs(parsed.query).get("title", [])
        if not titles:
            return None
        title = unquote(titles[0].split("#", 1)[0])
    else:
        return None

    title = _normalize_title(title)
    if not title or title.startswith("Special:"):
        return None

    return lang, title


def is_wikipedia_url(url: str) -> bool:
    """True when the URL host is ``*.wikipedia.org`` (including mobile)."""
    host = (urlparse(url).hostname or "").lower()
    return _WIKI_HOST.match(host) is not None


def is_wikipedia_special(url: str) -> bool:
    """True when the URL is a Wikipedia host with a ``Special:`` page title."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not _WIKI_HOST.match(host):
        return False

    path = parsed.path
    if path.startswith("/wiki/"):
        title = unquote(path[len("/wiki/") :].split("#", 1)[0])
    elif path.rstrip("/") == "/w/index.php" or path.endswith("/w/index.php"):
        titles = parse_qs(parsed.query).get("title", [])
        if not titles:
            return False
        title = unquote(titles[0].split("#", 1)[0])
    else:
        return False

    return _normalize_title(title).startswith("Special:")


def _api_params(title: str) -> dict[str, str]:
    return {
        "action": "query",
        "format": "json",
        "prop": "extracts",
        "explaintext": "1",
        "exintro": "0",
        "exchars": "1000000",
        "redirects": "1",
        "titles": title,
    }


def _api_endpoint(lang: str) -> str:
    return f"https://{lang}.wikipedia.org{_API_ENDPOINT}"


def _wikipedia_headers() -> dict[str, str]:
    ua = cfg.fetch_user_agent
    return {
        "User-Agent": ua,
        "Api-User-Agent": ua,
    }


def _extract_page_text(data: dict) -> str | None:
    pages = (data.get("query") or {}).get("pages") or {}
    for page in pages.values():
        if page.get("missing") is not None:
            return None
        extract = (page.get("extract") or "").strip()
        if extract:
            return extract
    return None


async def fetch_via_wikipedia_api(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
) -> FetchResult | None:
    """Fetch article text via the MediaWiki Action API, or ``None`` on failure."""
    parsed = parse_wikipedia_url(url)
    if parsed is None:
        return None

    lang, title = parsed
    api_url = _api_endpoint(lang)

    await _limiter.wait(api_url)
    try:
        resp = await client.post(
            api_url,
            data=_api_params(title),
            headers=_wikipedia_headers(),
            follow_redirects=True,
            timeout=_http_timeout(),
        )
    except httpx.TimeoutException:
        return FetchResult(doc_id, url, "unfetchable", None, None, "timeout")
    except httpx.RequestError as exc:
        return FetchResult(doc_id, url, "unfetchable", None, None, str(exc))

    http_status = resp.status_code
    if http_status >= 400:
        return FetchResult(
            doc_id, url, "unfetchable", None, http_status, f"HTTP {http_status}"
        )

    try:
        data = resp.json()
    except ValueError:
        return FetchResult(
            doc_id, url, "unfetchable", None, http_status, "invalid json response"
        )

    text = _extract_page_text(data)
    if not text:
        return FetchResult(
            doc_id, url, "unfetchable", None, http_status, "wikipedia page missing or empty"
        )

    return FetchResult(
        doc_id,
        url,
        "fetched",
        text,
        http_status,
        "fetched via wikipedia api",
    )


async def fetch_wikipedia_with_retries(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
) -> FetchResult:
    """Try the Wikipedia API up to ``1 + fetch_wikipedia_max_retries`` times."""
    max_retries = cfg.fetch_wikipedia_max_retries
    delay = cfg.fetch_wikipedia_retry_delay_seconds
    last: FetchResult | None = None

    for attempt in range(max_retries + 1):
        result = await fetch_via_wikipedia_api(client, doc_id, url)
        if result is None:
            return FetchResult(
                doc_id, url, "unfetchable", None, None, "not a wikipedia article url"
            )
        if result.status == "fetched":
            return result
        last = result
        if attempt < max_retries:
            log.debug(
                "Wikipedia API attempt %d failed for %s: %s; retrying in %ss",
                attempt + 1,
                url,
                result.error_msg,
                delay,
            )
            await asyncio.sleep(delay)

    assert last is not None
    return last
