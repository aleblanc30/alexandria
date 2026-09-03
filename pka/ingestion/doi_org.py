"""``doi.org`` bookmark → DOI content negotiation, instead of following the redirect.

A bookmarked ``https://doi.org/10.1016/j.artint.2018.07.007`` is today fetched
by following the redirect and scraping whatever the publisher serves — usually a
paywall or a cookie wall, at HTTP 200, which is then chunked and embedded as if
it were the paper. Resolving the DOI to structured metadata instead removes that
silent failure and costs one request.

Content negotiation is the right primary *here* specifically: the bookmarked
host is ``doi.org``, so no third-party question arises (DESIGN.md §1.1), and it
is registration-agency agnostic — a DataCite DOI (Zenodo dataset, figshare item)
answers where a Crossref-only client would 404. The publisher handlers use
``api.crossref.org`` instead, for the query support they need.

Two ``doi.org``-specific defects this also removes, worth recording because both
look like bugs elsewhere:

- The rate-limiter slot is claimed against ``doi.org``, not the host the
  redirect lands on, so a folder of ``doi.org/10.1016/…`` links used to hammer
  ``sciencedirect.com`` with no per-domain spacing.
- On a 404 the Wayback fallback queried archive.org for a snapshot of the
  *``doi.org`` URL* — a redirect stub, never the content anyone wanted.

``hdl.handle.net`` is out of scope: it is the wider Handle system, DOIs are one
namespace inside it, and non-DOI handles have no metadata contract.
"""

from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import unquote, urlparse

import httpx

from pka.ingestion.doi_meta import fetch_doi_card
from pka.ingestion.fetch_base import FetchResult

_DOI_HOST = re.compile(r"^(?:www\.)?(?:dx\.)?doi\.org$", re.IGNORECASE)
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
_ARXIV_DOI_PREFIX = "10.48550/arxiv."


def is_doi_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(_DOI_HOST.match(host))


def parse_doi_url(url: str) -> str | None:
    """Return the DOI a ``doi.org`` URL resolves, or ``None``.

    A **prefix strip, not a path split**: DOI suffixes contain slashes
    (``10.1103/PhysRevLett.116.061102``), so everything after the leading ``/``
    is the identifier. ``doi.org/`` and ``doi.org/about`` return ``None`` and
    fall through to the generic path.

    The DOI is returned **as parsed**, not lowercased. Resolution is
    case-insensitive so it makes no difference to the request, but
    ``normalize_doi`` lowercases for the ``documents.doi`` column and mixing the
    two invites a later "simplification" that reuses the column value in a URL.
    """
    if not is_doi_host(url):
        return None
    parsed = urlparse(url)
    candidate = unquote(parsed.path or "").lstrip("/").rstrip("/")
    if parsed.query:
        # Some exporters append tracking parameters; the DOI ends before them.
        candidate = candidate.split("?", 1)[0]
    return candidate if _DOI_RE.match(candidate) else None


async def fetch_doi_url(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
) -> FetchResult | None:
    """Resolve a ``doi.org`` bookmark. ``None`` when the URL is not one."""
    doi = parse_doi_url(url)
    if not doi:
        return None

    if doi.lower().startswith(_ARXIV_DOI_PREFIX):
        # arXiv mints a DOI for every submission, and arxiv.py yields full PDF
        # text where this handler yields an abstract — so hand the cross-walk
        # over rather than settling for the thinner card.
        from pka.ingestion.arxiv import fetch_arxiv_paper

        arxiv_id = doi[len(_ARXIV_DOI_PREFIX) :]
        result = await fetch_arxiv_paper(client, doc_id, f"https://arxiv.org/abs/{arxiv_id}")
        if result is not None and result.status == "fetched":
            # Keep the bookmark's own URL on the row; only the text is arXiv's.
            return replace(result, url=url)

    return await fetch_doi_card(client, doc_id, url, doi, via="doi.org", negotiated=True)
