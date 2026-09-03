"""``nature.com`` article → DOI by string concatenation, then ``doi_meta``.

Nature article URLs carry the DOI *suffix* verbatim and the prefix ``10.1038``
is constant across everything Springer Nature publishes on this host, so there
is no lookup step in the derivation — it is concatenation. Three path shapes:

===================  ==========================================  ======================
Shape                Example                                     DOI
===================  ==========================================  ======================
Modern               ``/articles/s41586-020-2649-2``             ``10.1038/s41586-020-2649-2``
Legacy numbered      ``/articles/nature12373``                   ``10.1038/nature12373``
Legacy journal tree  ``/nature/journal/v491/n7422/full/nature11421.html``  ``10.1038/nature11421``
===================  ==========================================  ======================

The third is a real archive shape — bookmarks outlive site redesigns.

The article-ID alphabet is accepted conservatively (``[A-Za-z0-9._-]+``, no
further slash). That one rule excludes every non-article path without
enumerating them — ``/subjects/genetics``, ``/nature/volumes/491``,
``/collections/…`` — and those then fall through to the generic path
**unchanged, which is correct**: a Nature subject index is a real, fetchable,
non-paywalled page that trafilatura handles fine.

News items (``d41586-`` DOIs) are in scope deliberately. They are registered
with Crossref, often carry no abstract — fine, the card degrades to
metadata-only — and are a large share of what lands in a bookmark folder from
this host. A title + byline + year card beats a paywall scrape.

**No PDF attempt.** ``nature.com/articles/<id>.pdf`` is paywalled for non-OA
content and returns an HTML interstitial at HTTP 200, which the PDF route then
rejects as "response is not a PDF" — a wasted request and a misleading
``fetch_log`` row.

``scientificamerican.com`` is excluded: same publisher, but its content has no
DOI and trafilatura reads it correctly.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from pka.ingestion.doi_meta import fetch_doi_card
from pka.ingestion.fetch_base import FetchResult

_NATURE_HOST = re.compile(r"^(?:www\.)?nature\.com$", re.IGNORECASE)
_NATURE_PREFIX = "10.1038"
# /articles/<id> and /<journal>/articles/<id>
_ARTICLE_PATH = re.compile(r"^/(?:[a-z0-9-]+/)?articles/([A-Za-z0-9._-]+)/?$", re.IGNORECASE)
# /<journal>/journal/vNNN/nNNNN/<view>/<id>.html
_LEGACY_PATH = re.compile(
    r"^/[a-z0-9-]+/journal/v[^/]+/n[^/]+/[a-z]+/([A-Za-z0-9._-]+)\.html?$",
    re.IGNORECASE,
)
_SUPPLEMENT_SUFFIX = re.compile(r"_[SF]\d+$", re.IGNORECASE)


def is_nature_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(_NATURE_HOST.match(host))


def _article_id(path: str) -> str | None:
    match = _ARTICLE_PATH.match(path)
    if match:
        article_id = match.group(1)
        if article_id.lower().endswith(".pdf"):
            article_id = article_id[:-4]
        return article_id or None
    match = _LEGACY_PATH.match(path)
    if match:
        # A supplementary figure or table resolves to its parent article.
        return _SUPPLEMENT_SUFFIX.sub("", match.group(1)) or None
    return None


def parse_nature_url(url: str) -> str | None:
    """Return the ``10.1038/…`` DOI for a Nature article URL, or ``None``."""
    if not is_nature_url(url):
        return None
    article_id = _article_id(urlparse(url).path or "")
    return f"{_NATURE_PREFIX}/{article_id}" if article_id else None


async def fetch_nature_article(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
) -> FetchResult | None:
    """Metadata card for a Nature article. ``None`` when the URL is not one."""
    doi = parse_nature_url(url)
    if not doi:
        return None
    return await fetch_doi_card(client, doc_id, url, doi, via="nature.com")
