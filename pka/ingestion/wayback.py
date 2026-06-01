"""Wayback Machine fallback when a Firefox bookmark URL returns HTTP 404."""
from __future__ import annotations

import logging
from urllib.parse import urlencode

import httpx

from pka.ingestion.fetcher import (
    _HTML_TYPES,
    FetchResult,
    _extract_text,
    _fetch_pdf_result,
    _http_timeout,
    _is_pdf_bytes,
    _is_pdf_content_type,
    _limiter,
    _url_looks_like_pdf,
)

log = logging.getLogger(__name__)

_AVAILABILITY_URL = "https://archive.org/wayback/available"


async def lookup_snapshot_url(
    client: httpx.AsyncClient,
    url: str,
) -> tuple[str, str] | None:
    """Return ``(snapshot_url, timestamp)`` when archive.org has a closest snapshot."""
    api_url = f"{_AVAILABILITY_URL}?{urlencode({'url': url})}"
    await _limiter.wait(api_url)
    try:
        resp = await client.get(
            api_url,
            follow_redirects=True,
            timeout=_http_timeout(),
        )
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        log.debug("Wayback availability lookup failed for %s: %s", url, exc)
        return None

    if resp.status_code >= 400:
        return None

    try:
        data = resp.json()
    except ValueError:
        return None

    closest = (data.get("archived_snapshots") or {}).get("closest") or {}
    if not closest.get("available"):
        return None

    snapshot_url = closest.get("url")
    timestamp = closest.get("timestamp")
    if not snapshot_url or not timestamp:
        return None

    return snapshot_url, str(timestamp)


async def fetch_via_wayback(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
) -> FetchResult | None:
    """Fetch content from the closest Wayback snapshot, or ``None`` if unavailable."""
    lookup = await lookup_snapshot_url(client, url)
    if lookup is None:
        return None

    snapshot_url, timestamp = lookup
    expect_pdf = _url_looks_like_pdf(url)

    await _limiter.wait(snapshot_url)
    try:
        resp = await client.get(
            snapshot_url,
            follow_redirects=True,
            timeout=_http_timeout(pdf=expect_pdf),
        )
    except httpx.TimeoutException:
        return None
    except httpx.RequestError as exc:
        log.debug("Wayback snapshot fetch failed for %s: %s", url, exc)
        return None

    http_status = resp.status_code
    if http_status >= 400:
        return None

    content_type = resp.headers.get("content-type", "").split(";")[0].strip()
    body = resp.content

    if expect_pdf or _is_pdf_content_type(content_type) or _is_pdf_bytes(body):
        result = _fetch_pdf_result(doc_id, url, body, http_status)
    elif content_type and content_type not in _HTML_TYPES:
        return None
    else:
        text = _extract_text(resp.text, snapshot_url)
        if not text:
            return None
        result = FetchResult(doc_id, url, "fetched", text, http_status, None)

    if result.status != "fetched":
        return None

    provenance = f"fetched via wayback snapshot {timestamp} (original HTTP 404)"
    return FetchResult(
        doc_id,
        url,
        "fetched",
        result.text,
        http_status,
        provenance,
        archive_url=snapshot_url,
    )
