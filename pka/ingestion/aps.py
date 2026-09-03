"""American Physical Society → DOI read straight out of the path.

APS answers an unauthenticated non-browser client with ``403``, so these
bookmarks currently land as ``unfetchable`` with no title, no abstract and no
chunks. The DOI is right there in the URL, so nothing needs to be scraped.

Two hosts, both in scope:

===================  =========================  ==============================================
Host                 Shape                      Example
===================  =========================  ==============================================
``journals.aps.org`` ``/<journal>/<view>/<DOI>`` ``/prl/abstract/10.1103/PhysRevLett.116.061102``
``link.aps.org``     ``/<view>/<DOI>``          ``/doi/10.1103/PhysRevLett.116.061102``
===================  =========================  ==============================================

``link.aps.org`` is APS's own redirector, common in citation lists and reference
managers. It is *not* ``doi.org`` and ``doi_org.py`` will not match it, so it is
handled here rather than left as a second unfetchable domain nobody opened a
TODO for.

Views in the wild (``abstract``, ``pdf``, ``accepted``, ``supplemental``,
``cited-by``, ``references``, ``export``, ``doi``) and journal slugs (``prl``,
``pra``…``prx``, ``rmp``, ``prper``, ``prapplied``, …) are deliberately not
enumerated: ``doi_from_path``'s positional scan handles all of them and needs no
maintenance when APS adds another.

The prefix is constrained to ``10.1103`` after the scan. APS mints nothing else,
and the check turns a malformed path into a clean fall-through instead of a
request for a DOI that cannot exist.

**Supplemental material is a deliberate merge, not a bug.**
``/supplemental/10.1103/PhysRevLett.116.061102`` resolves to the article record,
so its card describes the paper rather than the supplement — which is the right
answer, the supplement having no independent metadata. Two such bookmarks then
produce two documents with identical titles; they stay distinct rows because
``source_id`` is the bookmark id, and that duplicate is understood, not broken.

One upstream data wrinkle, recorded so a reviewer does not "fix" it: Crossref's
APS abstracts end with *"Published by the American Physical Society 2016"* and
render inline mathematics as spaced Unicode. No per-publisher scrubbing is done
here — ``preprint_card_summary`` cleans nothing for arXiv or PubMed either, and
per-publisher rules are how a module like this starts growing.

The arXiv cross-walk (nearly every APS paper has a preprint whose PDF
``arxiv.py`` can read in full) is deliberately *not* here: it forces the
Semantic Scholar request on every APS URL and then adds an API call and a PDF
download, and storing the preprint under the journal DOI is a provenance claim
``documents`` has no column to qualify. See ``PUBLISHER_FETCH_HANDLERS.md`` §7.1.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from pka.ingestion.doi_meta import doi_from_path, fetch_doi_card
from pka.ingestion.fetch_base import FetchResult

_APS_HOST = re.compile(r"^(?:www\.)?(?:journals|link)\.aps\.org$", re.IGNORECASE)
_APS_PREFIX = "10.1103"


def is_aps_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(_APS_HOST.match(host))


def parse_aps_url(url: str) -> str | None:
    """Return the ``10.1103/…`` DOI in an APS URL, or ``None``."""
    if not is_aps_url(url):
        return None
    return doi_from_path(urlparse(url).path or "", prefix=_APS_PREFIX)


async def fetch_aps_article(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
) -> FetchResult | None:
    """Metadata card for an APS article. ``None`` when the URL is not one."""
    doi = parse_aps_url(url)
    if not doi:
        return None
    return await fetch_doi_card(client, doc_id, url, doi, via="aps")
