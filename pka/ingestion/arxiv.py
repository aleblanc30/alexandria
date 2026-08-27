"""arXiv API + PDF fetch for Firefox bookmark URLs."""
from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
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

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
_ARXIV_HOST = re.compile(r"^(?:www\.)?arxiv\.org$", re.IGNORECASE)
_ARXIV_PATH = re.compile(
    r"/(?:abs|pdf|html|e-print)/([^/?#]+?)(?:\.pdf)?/?(?:[?#].*)?$",
    re.IGNORECASE,
)
_VERSION_SUFFIX = re.compile(r"v\d+$", re.IGNORECASE)


@dataclass(frozen=True)
class ArxivMetadata:
    arxiv_id: str
    title: str
    authors: list[str]
    abstract: str
    categories: list[str]


def is_arxiv_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(_ARXIV_HOST.match(host))


def normalize_arxiv_id(raw: str) -> str:
    value = raw.strip().rstrip("/")
    if _VERSION_SUFFIX.search(value):
        value = _VERSION_SUFFIX.sub("", value)
    return value


def parse_arxiv_url(url: str) -> str | None:
    """Return normalized arXiv ID for a fetchable arxiv.org URL, or ``None``."""
    if not is_arxiv_url(url):
        return None
    match = _ARXIV_PATH.search(urlparse(url).path)
    if not match:
        return None
    arxiv_id = normalize_arxiv_id(match.group(1))
    return arxiv_id or None


def arxiv_api_url(arxiv_id: str) -> str:
    return f"https://export.arxiv.org/api/query?id_list={arxiv_id}"


def arxiv_pdf_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/pdf/{arxiv_id}.pdf"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse_arxiv_atom(xml_text: str) -> ArxivMetadata | None:
    """Parse the first Atom entry from an arXiv API response."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    entry = root.find("atom:entry", _ATOM_NS)
    if entry is None:
        for child in root:
            if _local_name(child.tag) == "entry":
                entry = child
                break
    if entry is None:
        return None

    def _find_text(parent: ET.Element, name: str) -> str:
        node = parent.find(f"atom:{name}", _ATOM_NS)
        if node is None:
            for child in parent:
                if _local_name(child.tag) == name:
                    node = child
                    break
        return (node.text or "").strip() if node is not None else ""

    title = _find_text(entry, "title").replace("\n", " ")
    summary = _find_text(entry, "summary")
    if not title or not summary:
        return None

    authors: list[str] = []
    for author in entry.findall("atom:author", _ATOM_NS) or []:
        name = _find_text(author, "name")
        if name:
            authors.append(name)
    if not authors:
        for child in entry:
            if _local_name(child.tag) == "author":
                name = _find_text(child, "name")
                if name:
                    authors.append(name)

    categories: list[str] = []
    for cat in entry.findall("atom:category", _ATOM_NS) or []:
        term = cat.get("term")
        if term:
            categories.append(term)
    for child in entry:
        if _local_name(child.tag) == "category" and child.get("term"):
            categories.append(child.get("term", ""))

    id_text = _find_text(entry, "id")
    arxiv_id = parse_arxiv_url(id_text) if id_text.startswith("http") else normalize_arxiv_id(id_text)
    if not arxiv_id:
        return None

    return ArxivMetadata(
        arxiv_id=arxiv_id,
        title=title,
        authors=authors,
        abstract=summary,
        categories=categories,
    )


async def _fetch_arxiv_metadata(
    client: httpx.AsyncClient,
    arxiv_id: str,
) -> tuple[ArxivMetadata | None, int | None, str | None]:
    api_url = arxiv_api_url(arxiv_id)
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

    meta = parse_arxiv_atom(resp.text)
    if meta is None:
        return None, resp.status_code, "arxiv api returned no entry"
    return meta, resp.status_code, None


async def _fetch_arxiv_pdf_text(
    client: httpx.AsyncClient,
    arxiv_id: str,
) -> tuple[str | None, int | None, str | None]:
    pdf_url = arxiv_pdf_url(arxiv_id)
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

    result = await asyncio.to_thread(
        _fetch_pdf_result, 0, pdf_url, resp.content, resp.status_code,
    )
    if result.status != "fetched" or not result.text:
        return None, resp.status_code, result.error_msg or "pdf extraction failed"
    return result.text, resp.status_code, None


async def fetch_arxiv_paper(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
) -> FetchResult | None:
    """Fetch arXiv metadata + PDF for a bookmark URL. Returns ``None`` when not an arXiv URL."""
    arxiv_id = parse_arxiv_url(url)
    if not arxiv_id:
        return None

    meta, http_status, err = await _fetch_arxiv_metadata(client, arxiv_id)
    if meta is None:
        return FetchResult(doc_id, url, "unfetchable", None, http_status, err)

    pdf_text, pdf_status, pdf_err = await _fetch_arxiv_pdf_text(client, meta.arxiv_id)
    card_summary = preprint_card_summary(meta.abstract)
    title = meta.title

    if pdf_text:
        text = build_preprint_text(
            title=meta.title,
            authors=meta.authors,
            abstract=meta.abstract,
            pdf_text=pdf_text,
        )
        msg = "fetched via arxiv api"
    elif meta.abstract:
        text = build_preprint_text(
            title=meta.title,
            authors=meta.authors,
            abstract=meta.abstract,
            pdf_text=None,
        )
        msg = "fetched via arxiv api (abstract only; pdf unavailable)"
        log.info("arXiv PDF unavailable for %s: %s", arxiv_id, pdf_err)
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
