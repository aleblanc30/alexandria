"""ScienceDirect → DOI by Crossref query, because the PII is opaque.

``/science/article/pii/S0004370218305988`` carries a PII, not a DOI, and nothing
in it derives ``10.1016/j.artint.2018.07.007`` — the mapping is not computable.
It *is* queryable: Elsevier deposits the PII to Crossref as an
``alternative-id``, and the REST API filters on it, returning exactly one work.

**And it is only a partial route.** Those deposits are not retroactive — the
same query for an older PII returns ``total-results: 0`` — so this handler has a
real miss rate concentrated in older bookmarks, and what happens then is part of
the design rather than an afterthought (see ``_url_card`` below).

Elsevier deposits no abstracts to Crossref at all, so **every** ScienceDirect
document takes the Semantic Scholar rung. The filter query therefore selects the
whole record in one call rather than resolving the DOI and fetching it again,
keeping the common case at two requests instead of three.

Hosts: ``sciencedirect.com`` and ``linkinghub.elsevier.com``, whose
``/retrieve/pii/<PII>`` form is the same identifier and appears wherever a
reference manager wrote the link. Path shapes: ``/science/article/pii/<PII>``,
``/science/article/abs/pii/<PII>``, ``/science/article/pii/<PII>/pdf(ft)``.
``/journal/artificial-intelligence``, ``/search?…`` and the bare host carry no
PII and fall through.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from pka.card_summary import truncate_summary
from pka.ingestion.doi_meta import (
    doi_result,
    enrich_abstract,
    fetch_crossref_alternative_id,
)
from pka.ingestion.fetch_base import FetchResult

_SD_HOST = re.compile(r"^(?:www\.)?sciencedirect\.com$", re.IGNORECASE)
_LINKINGHUB_HOST = re.compile(r"^(?:www\.)?linkinghub\.elsevier\.com$", re.IGNORECASE)
# `S` for serials, `B` for book chapters.
_PII_RE = re.compile(r"^[SB]\d{8,}[0-9X]$", re.IGNORECASE)
_SD_PATH = re.compile(r"^/science/article/(?:abs/)?pii/([^/]+)", re.IGNORECASE)
_LINKINGHUB_PATH = re.compile(r"^/retrieve/pii/([^/]+)", re.IGNORECASE)


def is_sciencedirect_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(_SD_HOST.match(host) or _LINKINGHUB_HOST.match(host))


def parse_sciencedirect_url(url: str) -> str | None:
    """Return the PII in a ScienceDirect / linkinghub URL, or ``None``."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if _SD_HOST.match(host):
        match = _SD_PATH.match(parsed.path or "")
    elif _LINKINGHUB_HOST.match(host):
        match = _LINKINGHUB_PATH.match(parsed.path or "")
    else:
        return None
    if not match:
        return None
    pii = match.group(1).upper()
    return pii if _PII_RE.match(pii) else None


def _url_card(doc_id: int, url: str, pii: str) -> FetchResult:
    """The card for a PII Crossref cannot resolve.

    Three options were weighed and this is the third. Falling through to the
    generic GET is the silent paywall ingest this handler exists to remove, and
    ``unfetchable`` leaves the document with no title at all — honest, but a
    bookmark that stays invisible. A URL-derived card (the ``search_url.py`` and
    ``reddit_bookmark`` precedent) is findable and honestly labelled.

    The caveat: ``status="fetched"`` means ``source_ingest_queue`` will not
    re-queue it, so a future Elsevier ``alternative-id`` backfill is not picked
    up automatically. That is the same trade ``search_url.py`` accepted, and for
    the same reason — a novel ``fetch_status`` silently drops the document out of
    orphan backfill entirely.
    """
    summary = truncate_summary(f"ScienceDirect article, Elsevier PII {pii}. No metadata record.")
    return FetchResult(
        doc_id,
        url,
        "fetched",
        f"{pii}\n\nScienceDirect article (Elsevier)",
        # No status: any 200 here belongs to the Crossref query that found
        # nothing, not to a request for this document.
        None,
        "sciencedirect pii unresolved; card built from url",
        card_summary=summary,
    )


async def fetch_sciencedirect_article(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
) -> FetchResult | None:
    """Metadata card for a ScienceDirect article. ``None`` when not one."""
    pii = parse_sciencedirect_url(url)
    if not pii:
        return None

    from pka.config import settings as cfg

    if not cfg.doi_metadata_lookup:
        return _url_card(doc_id, url, pii)

    meta, http_status, err = await fetch_crossref_alternative_id(client, pii)
    if meta is None:
        return _url_card(doc_id, url, pii)

    lookup = await enrich_abstract(client, meta, http_status)
    return doi_result(doc_id, url, lookup, via="sciencedirect")
