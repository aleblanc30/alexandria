"""``mitpress.mit.edu`` → ISBN, not DOI. A bookstore, not a journal.

This is the handler most likely to be built wrong by analogy with its six
neighbours in ``planning/archive/PUBLISHER_FETCH_HANDLERS.md``. ``mitpress.mit.edu``
serves book *product* pages, with no DOI anywhere::

    https://mitpress.mit.edu/9780262369466/advanced-microeconomics-…/
                             └─── ISBN-13 ───┘ └──── title slug ────┘

So this is a sibling of ``amazon.py``, and the identifier resolves through the
archive's existing, tested ISBN ladder in ``openlibrary.py`` rather than through
``doi_meta``. The slugless form (``/9780262192026/``) is also in the wild.

Three consequences specific to this handler:

**It crosses the sync/async boundary.** ``openlibrary.py`` is deliberately
synchronous, with its own ``SyncRateLimiter``, because its existing callers
(``image_pipeline``, ``runners/calibre``) are synchronous. The fetch pool is
async, so the lookup goes through ``asyncio.to_thread`` exactly as
``_fetch_pdf_result`` and ``extract_amazon_book`` already do. Rewriting
``openlibrary.py`` as async to suit one new caller would be the wrong direction.

**It is gated off by default, and that is correct.**
``external_lookup_enabled`` defaults to ``False``, so on a fresh checkout this
handler makes **no request at all** — and still improves on today's ``403``:
title from the slug, ISBN from the path, ``status="fetched"``. The Open Library
lookup is a strict upgrade layered on when the flag is on. Per DESIGN.md §1.1
this needs no new flag; it is another consumer of an existing one, and no
implicit escalation occurs.

**A bad-checksum ISBN is never written.** ``documents.isbn`` is a join key, so
the path digits are validated with ``normalize_isbn`` / ``isbn_checksum_valid``
before they reach the column; the slug card is still built.

``direct.mit.edu`` is a *different host* — MIT Press's journals gateway, which
mints ``10.1162`` DOIs — and is deliberately not handled here or anywhere. Its
article paths (``/neco/article/9/8/1735/6109/<slug>``) carry no DOI, so the only
derivation route would be scraping the page's ``citation_doi`` meta tag, and
that route is closed: probed 2026-09-03, both an article page and a book page
answer a non-browser client with ``403``. You cannot read a meta tag out of a
page you cannot fetch. So it falls through to the generic path and stays
``unfetchable`` — the honest outcome, and the same one it has today.

What the URL *does* carry is a title slug, which is the ``researchgate.py``
shape: a card built from the slug with no request. That is a separate, cheap
improvement, not a DOI handler, and it is not built here.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import unquote, urlparse

from pka.card_summary import truncate_summary
from pka.config import settings as cfg
from pka.ingestion.fetch_base import FetchResult
from pka.ingestion.openlibrary import isbn_checksum_valid, normalize_isbn

_MITPRESS_HOST = re.compile(r"^(?:www\.)?mitpress\.mit\.edu$", re.IGNORECASE)
_BOOK_PATH = re.compile(r"^/(97[89]\d{10})(?:/([^/]*))?/?$")


def is_mitpress_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(_MITPRESS_HOST.match(host))


def parse_mitpress_url(url: str) -> tuple[str, str | None] | None:
    """Return ``(isbn13, slug_or_none)`` for an MIT Press book page."""
    if not is_mitpress_url(url):
        return None
    match = _BOOK_PATH.match(unquote(urlparse(url).path or ""))
    if not match:
        return None
    slug = (match.group(2) or "").strip() or None
    return match.group(1), slug


def title_from_slug(slug: str | None) -> str | None:
    """``raised-to-rage`` → ``Raised To Rage``. ``None`` for a slugless URL."""
    if not slug:
        return None
    words = [w for w in re.split(r"[-_]+", slug) if w]
    if not words:
        return None
    return truncate_summary(" ".join(w[:1].upper() + w[1:] for w in words))


async def fetch_mitpress_book(
    client: object,
    doc_id: int,
    url: str,
) -> FetchResult | None:
    """Card for an MIT Press book page. ``None`` when the URL is not one.

    ``client`` is accepted and unused: the dispatch chain hands one to every
    async handler, and the Open Library leg brings its own HTTP client.
    """
    parsed = parse_mitpress_url(url)
    if parsed is None:
        return None
    raw_isbn, slug = parsed

    isbn = normalize_isbn(raw_isbn)
    if isbn is not None and not isbn_checksum_valid(isbn):
        isbn = None  # a join key, so a bad checksum is dropped rather than stored

    title = title_from_slug(slug)
    synopsis = None
    if isbn and cfg.external_lookup_enabled:
        from pka.ingestion.openlibrary import lookup_by_isbn

        synopsis = await asyncio.to_thread(lookup_by_isbn, isbn)

    if synopsis is not None:
        title = synopsis.title or title
        card_summary = truncate_summary(synopsis.description)
        text = "\n\n".join(part for part in (title, synopsis.description) if part)
        msg = "fetched via mitpress isbn → open library"
    else:
        label = title or f"MIT Press book {raw_isbn}"
        card_summary = truncate_summary(f"MIT Press book, ISBN {raw_isbn}.")
        text = f"{label}\n\nMIT Press book"
        msg = "mitpress; card built from url"
        if isbn and cfg.external_lookup_enabled:
            msg += " (open library had no description)"

    return FetchResult(
        doc_id,
        url,
        "fetched",
        text,
        None,
        msg,
        title=title,
        card_summary=card_summary,
        isbn=isbn,
    )
