"""``link.springer.com`` → DOI read straight out of the path.

SpringerLink returns ``200`` with a paywall, which the generic path extracts and
embeds as though it were the paper — the silent half of the publisher problem.
The DOI is simply there in the URL, prefix and suffix both:
``/article/10.1007/s11263-015-0816-y``.

Content segments seen in the wild are ``/article/``, ``/chapter/``, ``/book/``,
``/referenceworkentry/``, ``/protocol/``, ``/content/pdf/`` (suffix ends
``.pdf``), ``/epdf/`` and ``/full/``. ``doi_from_path``'s positional scan covers
all of them and any future addition, so none of them is enumerated here.

``/content/pdf/`` is in scope rather than excluded, and the asymmetry is worth
naming: when the bookmark itself is a ``/content/pdf/…`` URL the generic path
already tries the PDF route and fails on the HTML interstitial, so intercepting
it and returning a real metadata card is a strict improvement.

Book DOIs contain hyphens and underscores (``978-3-030-01234-5_7``); the suffix
is never "cleaned" beyond the trailing ``.pdf`` and ``/`` strips.

Index pages — ``/journal/11263``, ``/search?…``, ``/collections/…``, the bare
host — carry no DOI, so they fall through to the generic path, which handles
them correctly.

Excluded hosts: ``springer.com`` (marketing, no DOIs), ``springeropen.com`` and
the BMC hosts (open access — the generic path already gets *full text* there,
and replacing that with an abstract would be a regression).

**Springer is the evidence that the abstract ladder is mandatory.** Crossref
returns no abstract for ``10.1007/s11263-015-0816-y`` while returning one for
``10.1038`` samples from the same publishing group; built "Crossref only,
it's simpler", a large slice of SpringerLink bookmarks would land as
abstract-less cards and the cause — a per-deposit metadata gap — would be
invisible. ``doi_meta.fetch_doi_metadata`` handles that second rung.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from pka.ingestion.doi_meta import doi_from_path, fetch_doi_card
from pka.ingestion.fetch_base import FetchResult

_SPRINGER_HOST = re.compile(r"^(?:www\.)?link\.springer\.com$", re.IGNORECASE)


def is_springer_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(_SPRINGER_HOST.match(host))


def parse_springer_url(url: str) -> str | None:
    """Return the DOI in a SpringerLink URL, or ``None``."""
    if not is_springer_url(url):
        return None
    return doi_from_path(urlparse(url).path or "")


async def fetch_springer_article(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
) -> FetchResult | None:
    """Metadata card for a SpringerLink item. ``None`` when the URL is not one."""
    doi = parse_springer_url(url)
    if not doi:
        return None
    return await fetch_doi_card(client, doc_id, url, doi, via="springer")
