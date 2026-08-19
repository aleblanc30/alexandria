"""bioRxiv API + PDF fetch for Firefox bookmark URLs."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from pka.card_summary import preprint_card_summary
from pka.ingestion.fetch_base import (
    FetchResult,
    _fetch_pdf_result,
    _http_timeout,
    _limiter,
)
from pka.ingestion.preprint_text import build_preprint_text

log = logging.getLogger(__name__)

_BIORXIV_HOST = re.compile(r"^(?:www\.)?biorxiv\.org$", re.IGNORECASE)
_DOI_RE = re.compile(r"(10\.1101/\S+?)(?:v\d+)?(?:\.full\.pdf|\.full|/|$)", re.IGNORECASE)
_EARLY_PATH = re.compile(
    r"/content/early/\d{4}/\d{2}/\d{2}/([\d.]+?)(?:v(\d+))?(?:\.full\.pdf|/|$)",
    re.IGNORECASE,
)
_VERSION_IN_PATH = re.compile(r"v(\d+)(?:\.full\.pdf|/|$)", re.IGNORECASE)


@dataclass(frozen=True)
class BiorxivMetadata:
    doi: str
    title: str
    authors: str
    abstract: str
    version: int
    category: str | None


def is_biorxiv_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(_BIORXIV_HOST.match(host))


def parse_biorxiv_url(url: str) -> tuple[str, int | None] | None:
    """Return ``(doi, version_or_none)`` for a bioRxiv content URL."""
    if not is_biorxiv_url(url):
        return None
    path = urlparse(url).path
    match = _DOI_RE.search(path)
    if match:
        doi = match.group(1).rstrip("/")
        version_match = _VERSION_IN_PATH.search(path)
        version = int(version_match.group(1)) if version_match else None
        return doi, version

    early = _EARLY_PATH.search(path)
    if early:
        doi = f"10.1101/{early.group(1)}"
        version = int(early.group(2)) if early.group(2) else None
        return doi, version
    return None


def biorxiv_detail_url(doi: str) -> str:
    return f"https://api.biorxiv.org/details/biorxiv/{doi}/na/json"


def biorxiv_pdf_url(doi: str, version: int) -> str:
    return f"https://www.biorxiv.org/content/{doi}v{version}.full.pdf"


def parse_biorxiv_detail(data: dict) -> BiorxivMetadata | None:
    collection = data.get("collection") or []
    if not collection:
        return None
    row = collection[0]
    title = (row.get("title") or "").strip()
    abstract = (row.get("abstract") or "").strip()
    doi = (row.get("doi") or "").strip()
    if not title or not abstract or not doi:
        return None
    version_raw = row.get("version")
    try:
        version = int(version_raw) if version_raw is not None else 1
    except (TypeError, ValueError):
        version = 1
    return BiorxivMetadata(
        doi=doi,
        title=title,
        authors=(row.get("authors") or "").strip(),
        abstract=abstract,
        version=version,
        category=(row.get("category") or None),
    )


async def _fetch_biorxiv_metadata(
    client: httpx.AsyncClient,
    doi: str,
) -> tuple[BiorxivMetadata | None, int | None, str | None]:
    api_url = biorxiv_detail_url(doi)
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

    meta = parse_biorxiv_detail(data)
    if meta is None:
        return None, resp.status_code, "biorxiv api returned no entry"
    return meta, resp.status_code, None


async def _fetch_biorxiv_pdf_text(
    client: httpx.AsyncClient,
    doi: str,
    version: int,
) -> tuple[str | None, int | None, str | None]:
    pdf_url = biorxiv_pdf_url(doi, version)
    await _limiter.wait(pdf_url)
    try:
        resp = await client.get(
            pdf_url,
            follow_redirects=True,
            timeout=_http_timeout(pdf=True),
        )
    except httpx.TimeoutException:
        return None, None, "pdf timeout"
    except httpx.RequestError as exc:
        return None, None, str(exc)

    if resp.status_code >= 400:
        return None, resp.status_code, f"pdf HTTP {resp.status_code}"

    result = _fetch_pdf_result(0, pdf_url, resp.content, resp.status_code)
    if result.status != "fetched" or not result.text:
        return None, resp.status_code, result.error_msg or "pdf extraction failed"
    return result.text, resp.status_code, None


async def fetch_biorxiv_paper(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
) -> FetchResult | None:
    """Fetch bioRxiv metadata + PDF for a bookmark URL. Returns ``None`` when not bioRxiv."""
    parsed = parse_biorxiv_url(url)
    if not parsed:
        return None
    doi, url_version = parsed

    meta, http_status, err = await _fetch_biorxiv_metadata(client, doi)
    if meta is None:
        return FetchResult(doc_id, url, "unfetchable", None, http_status, err)

    version = url_version or meta.version
    pdf_text, pdf_status, pdf_err = await _fetch_biorxiv_pdf_text(client, meta.doi, version)
    card_summary = preprint_card_summary(meta.abstract)
    title = meta.title

    if pdf_text:
        text = build_preprint_text(
            title=meta.title,
            authors=meta.authors,
            abstract=meta.abstract,
            pdf_text=pdf_text,
        )
        msg = "fetched via biorxiv api"
    elif meta.abstract:
        text = build_preprint_text(
            title=meta.title,
            authors=meta.authors,
            abstract=meta.abstract,
            pdf_text=None,
        )
        msg = "fetched via biorxiv api (abstract only; pdf unavailable)"
        log.info("bioRxiv PDF unavailable for %s: %s", doi, pdf_err)
    else:
        return FetchResult(
            doc_id,
            url,
            "unfetchable",
            None,
            pdf_status or http_status,
            pdf_err or "no abstract or pdf text",
        )

    return FetchResult(
        doc_id,
        url,
        "fetched",
        text,
        http_status,
        msg,
        title=title,
        card_summary=card_summary,
    )
