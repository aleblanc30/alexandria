"""PubMed E-utilities fetch for Firefox bookmark URLs.

Scope is PubMed **abstract** pages only (``pubmed.ncbi.nlm.nih.gov/<pmid>/`` and
the legacy ``ncbi.nlm.nih.gov/pubmed/<pmid>`` form). PubMed never hosts full
text itself, so this handler is metadata + abstract only — no PDF fetch, unlike
``arxiv.py`` / ``biorxiv.py``. See ``planning/FIREFOX_INGESTERS_PLAN.md`` §4 for
the full design.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from pka.card_summary import preprint_card_summary
from pka.ingestion.fetch_base import FetchResult, _http_timeout, _limiter
from pka.ingestion.identifiers import normalize_doi
from pka.ingestion.preprint_text import build_preprint_text

_PUBMED_HOST = re.compile(r"^pubmed\.ncbi\.nlm\.nih\.gov$", re.IGNORECASE)
_LEGACY_HOST = re.compile(r"^(?:www\.)?ncbi\.nlm\.nih\.gov$", re.IGNORECASE)
_PUBMED_PATH = re.compile(r"^/(\d+)/?$")
_LEGACY_PATH = re.compile(r"^/pubmed/(\d+)/?$", re.IGNORECASE)


@dataclass(frozen=True)
class PubmedMetadata:
    pmid: str
    title: str
    authors: list[str]  # "LastName Initials", e.g. "Smith A"
    abstract: str
    year: int | None
    doi: str | None  # None when ArticleIdList carries no doi entry — not a failure


def is_pubmed_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(_PUBMED_HOST.match(host) or _LEGACY_HOST.match(host))


def parse_pubmed_url(url: str) -> str | None:
    """Return the PMID for a fetchable PubMed abstract-page URL, or ``None``."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if _PUBMED_HOST.match(host):
        match = _PUBMED_PATH.match(parsed.path)
    elif _LEGACY_HOST.match(host):
        match = _LEGACY_PATH.match(parsed.path)
    else:
        return None
    return match.group(1) if match else None


def pubmed_efetch_url(pmid: str) -> str:
    return (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        f"?db=pubmed&id={pmid}&rettype=abstract&retmode=xml"
    )


def _abstract_text(article: ET.Element) -> str:
    """Join ``AbstractText`` nodes, prefixing each with its ``Label`` when present.

    A structured abstract repeats the element with e.g. ``Label="BACKGROUND"``;
    an ordinary abstract has exactly one, unlabeled, node.
    """
    parts: list[str] = []
    for node in article.findall("MedlineCitation/Article/Abstract/AbstractText"):
        text = " ".join("".join(node.itertext()).split())
        if not text:
            continue
        label = node.get("Label")
        parts.append(f"{label.capitalize()}: {text}" if label else text)
    return " ".join(parts)


def _authors(article: ET.Element) -> list[str]:
    authors: list[str] = []
    for author in article.findall("MedlineCitation/Article/AuthorList/Author"):
        last_name = (author.findtext("LastName") or "").strip()
        if not last_name:
            continue
        initials = (author.findtext("Initials") or "").strip()
        authors.append(f"{last_name} {initials}".strip())
    return authors


def _year(article: ET.Element) -> int | None:
    raw = article.findtext("MedlineCitation/Article/Journal/JournalIssue/PubDate/Year")
    return int(raw) if raw and raw.strip().isdigit() else None


def parse_pubmed_xml(xml_text: str) -> PubmedMetadata | None:
    """Parse the first ``PubmedArticle`` from an efetch XML response."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    article = root.find("PubmedArticle")
    if article is None:
        return None

    pmid = (article.findtext("MedlineCitation/PMID") or "").strip()
    title = (article.findtext("MedlineCitation/Article/ArticleTitle") or "").strip()
    abstract = _abstract_text(article)
    if not title or not abstract:
        return None

    doi = normalize_doi(article.findtext("PubmedData/ArticleIdList/ArticleId[@IdType='doi']"))

    return PubmedMetadata(
        pmid=pmid,
        title=title,
        authors=_authors(article),
        abstract=abstract,
        year=_year(article),
        doi=doi,
    )


async def _fetch_pubmed_metadata(
    client: httpx.AsyncClient,
    pmid: str,
) -> tuple[PubmedMetadata | None, int | None, str | None]:
    api_url = pubmed_efetch_url(pmid)
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

    meta = parse_pubmed_xml(resp.text)
    if meta is None:
        return None, resp.status_code, "pubmed efetch returned no entry"
    return meta, resp.status_code, None


async def fetch_pubmed_article(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
) -> FetchResult | None:
    """Fetch PubMed metadata + abstract for a bookmark URL.

    Returns ``None`` when ``url`` is not a PubMed abstract-page URL (dispatch
    in ``pka/ingestion/fetcher.py`` falls through to the next handler).
    """
    pmid = parse_pubmed_url(url)
    if not pmid:
        return None

    meta, http_status, err = await _fetch_pubmed_metadata(client, pmid)
    if meta is None:
        return FetchResult(doc_id, url, "unfetchable", None, http_status, err)

    text = build_preprint_text(
        title=meta.title,
        authors=meta.authors,
        abstract=meta.abstract,
        pdf_text=None,
    )
    authors_json = json.dumps(meta.authors) if meta.authors else None

    return FetchResult(
        doc_id,
        url,
        "fetched",
        text,
        http_status,
        "fetched via pubmed efetch",
        title=meta.title,
        card_summary=preprint_card_summary(meta.abstract),
        doi=meta.doi,
        year=meta.year,
        authors_json=authors_json,
    )
