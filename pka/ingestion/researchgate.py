"""ResearchGate URL → card, with no HTTP request at all.

ResearchGate is hard-blocked: Cloudflare bot protection, ``403`` to anything
that is not a real browser, and no public API. Every technique that would get
past it — TLS fingerprint spoofing, a headless browser, proxy rotation — is
anti-bot evasion, which does not belong in a local-first research archive.
Getting content *out of* ResearchGate is not the goal; making the bookmark
useful is.

And the bookmark already carries the payload, in the slug::

    researchgate.net/publication/334080242_403_Forbidden_A_Global_View_of_CDN_Geoblocking
                                 └──ID───┘ └──────────── title, underscored ────────────┘

So this follows ``search_url.py``, not ``arxiv.py``: a **synchronous handler
that takes no client and makes no request** — no rate-limiter slot, no budget
leg, no config flag.

The slug's casing is left alone. RG slugs preserve the paper's original
capitalisation, and title-casing would corrupt acronyms (``CDN``, ``403``).

``status="fetched"`` rather than a novel status, for ``search_url.py``'s reason:
``source_ingest_queue`` re-queues only ``pending`` documents and ``fetched`` ones
missing chunks, so a new status would silently drop the document out of orphan
backfill.

Other RG path shapes — ``/profile/<Name>``, ``/figure/…``, ``/post/…``,
``/institution/…`` — return ``None`` and fall through. They are not documents,
and they will keep failing with ``403`` as they do today; that is the correct
outcome and this handler should not pretend otherwise.

Title-matching the slug against Crossref to attach a real abstract was
considered and rejected for this slice, on ``openlibrary.py``'s own argument:
accepting an unverified rank-1 match is how the wrong paper's abstract gets
attached, which shifts ``doc_embedding`` and makes the document findable under
the wrong queries. If it is ever built it needs that module's verified
round-trip and its own default-off flag.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from pka.card_summary import SUMMARY_MAX_LEN, truncate_summary
from pka.ingestion.fetch_base import FetchResult

_RG_HOST = re.compile(r"^(?:www\.)?researchgate\.net$", re.IGNORECASE)
_PUBLICATION_PATH = re.compile(r"^/publication/(\d+)_(.+)$")
# Room for the "ResearchGate publication: " prefix and the trailing period.
_TITLE_MAX_LEN = SUMMARY_MAX_LEN - 30


def is_researchgate_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(_RG_HOST.match(host))


def parse_researchgate_url(url: str) -> tuple[str, str] | None:
    """Return ``(publication_id, title)`` for an RG publication URL, or ``None``."""
    if not is_researchgate_url(url):
        return None
    match = _PUBLICATION_PATH.match(unquote(urlparse(url).path or ""))
    if not match:
        return None
    slug = match.group(2).rstrip("/")
    for suffix in (".html", ".pdf"):
        if slug.lower().endswith(suffix):
            slug = slug[: -len(suffix)]
    title = truncate_summary(slug.replace("_", " "), _TITLE_MAX_LEN)
    if not title:
        return None
    return match.group(1), title


def researchgate_result(doc_id: int, url: str) -> FetchResult | None:
    """Build a card straight from an RG publication slug — no HTTP request.

    Returns ``None`` when ``url`` is not an RG publication URL (dispatch in
    ``pka/ingestion/fetcher.py`` falls through to the next handler).
    """
    parsed = parse_researchgate_url(url)
    if parsed is None:
        return None
    _publication_id, title = parsed

    return FetchResult(
        doc_id,
        url,
        "fetched",
        f"{title}\n\nResearchGate publication",
        None,
        "researchgate; card built from url slug, no fetch",
        title=title,
        # Set explicitly so embed_fetched_text does not fall back to
        # body_excerpt() over the two-line text above.
        card_summary=f"ResearchGate publication: {title}.",
    )
