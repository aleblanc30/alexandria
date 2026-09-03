"""``direct.mit.edu`` → Crossref by verified citation, or Open Library by title.

MIT Press's journals-and-books gateway is a hard block: probed 2026-09-03, both
``/neco/article/9/8/1735/6109/Long-Short-Term-Memory`` and
``/books/monograph/2313/The-Alignment-Problem`` answer a non-browser client with
``403``. So the ``citation_doi`` meta tag ``PUBLISHER_FETCH_HANDLERS.md`` §9
floated is not a route — a tag cannot be read out of a page that never loads —
and without a handler these bookmarks stay ``unfetchable`` with no title at all.

The URL still carries enough to identify the work, in two different shapes.

**Articles.** ``/neco/article/<volume>/<issue>/<page>/<article-id>/<slug>``. Those
numbers really are the ones in the DOI — ``10.1162/neco.1997.9.8.1735`` is
``<journal>.<year>.<volume>.<issue>.<page>`` — but the *year* is not in the URL,
and modern deposits abandon the pattern entirely (``10.1162/neco_a_01227``). So
the DOI cannot be concatenated the way ``nature.py`` does it.

It can be **searched and then verified**, which is a different thing.
``query.bibliographic=<slug title>`` scoped by ``filter=prefix:10.1162`` ranks
the right work first, and the URL independently supplies volume, issue and first
page to check it against. That check is the whole justification for this module:
a bibliographic query is a ranked guess, and ``openlibrary.py``'s docstring
already argues why accepting rank 1 unverified is worse than accepting nothing —
it attaches the wrong work's abstract, which shifts ``doc_embedding`` and makes
the document findable under the wrong queries. It is also why
``researchgate.py`` refuses this same route: an RG slug offers *nothing* to
round-trip against, whereas here three independent fields must agree. Rank 2 of
the live query — same journal, same volume, different issue and page — is
rejected by exactly that rule.

The prefix filter does the journal-scoping job without a journal-to-ISSN table,
which is the kind of list that rots quietly as a publisher adds titles.

**Books.** ``/books/<kind>/<id>/<slug>`` carries no identifier at all, only a
title — so it goes to ``openlibrary.lookup_by_title_author``, the archive's
existing verified-round-trip ladder, the same authorless rung Calibre uses for a
book with no ISBN. Gated on ``external_lookup_enabled`` (default off) like every
other consumer of that ladder, and reached through ``asyncio.to_thread`` because
``openlibrary.py`` is deliberately synchronous.

The two slug-bearing shapes fall back to a card built from that slug — never to
the generic GET, which is a guaranteed ``403``. Anything else on the host (a journal index,
a search page) returns ``None`` and falls through, failing as it does today.

**Article PDFs are a third shape, and the cheapest.** An ``article-pdf`` URL
ends in a filename rather than a title slug, and that filename is the DOI suffix
verbatim — in the legacy form (``neco.1997.9.8.1735.pdf``) and in the modern one
(``neco_a_01227.pdf``), which is why it does not matter that the two DOI schemes
differ. So this shape needs no search at all: prepend the prefix and look the
DOI up directly, one request. The derivation is still checked against the URL's
volume/issue/page, so a filename that turns out to name a different work is
rejected rather than trusted. A miss falls through rather than producing a card,
because this URL shape carries no title — there is nothing honest to put on one.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import httpx

from pka.card_summary import SUMMARY_MAX_LEN, truncate_summary
from pka.config import settings as cfg
from pka.ingestion.doi_meta import (
    doi_result,
    enrich_abstract,
    fetch_crossref_bibliographic,
    fetch_crossref_work_item,
)
from pka.ingestion.doi_meta import parse_doi_record as _parse_doi_record
from pka.ingestion.fetch_base import FetchResult

log = logging.getLogger(__name__)

_HOST = re.compile(r"^(?:www\.)?direct\.mit\.edu$", re.IGNORECASE)
_MIT_PRESS_PREFIX = "10.1162"
_ARTICLE_PATH = re.compile(
    r"^/(?P<journal>[a-z0-9_-]+)/article(?:-abstract|-pdf|-standard|-split)?"
    r"/(?P<volume>[0-9]+)/(?P<issue>[0-9A-Za-z-]+)/(?P<page>[0-9]+)"
    r"/(?P<article_id>[0-9]+)(?:/(?P<slug>[^/]*))?/?$",
    re.IGNORECASE,
)
_BOOK_PATH = re.compile(
    r"^/books/(?:monograph|edited-volume|book|oa-monograph|oa-edited-volume)"
    r"/(?P<id>[0-9]+)/(?P<slug>[^/]+?)/?$",
    re.IGNORECASE,
)
# A real DOI suffix carries an inner "." or "_" (neco.1997.9.8.1735,
# neco_a_01227); a bare "paper.pdf" does not.
_PDF_STEM_RE = re.compile(r"^[a-z][a-z0-9]*[._][a-z0-9._-]+$", re.IGNORECASE)
_TITLE_MAX_LEN = SUMMARY_MAX_LEN - 40


@dataclass(frozen=True)
class DirectMitArticle:
    """The citation coordinates an article URL hands over.

    ``doi`` is set only for the ``article-pdf`` shape, whose filename *is* the
    DOI suffix; ``title`` is then empty, because that URL carries no title slug.
    Exactly one of the two is always present.
    """

    journal: str
    volume: str
    issue: str
    page: str
    title: str
    doi: str | None = None


def is_direct_mit_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(_HOST.match(host))


def slug_title(slug: str | None) -> str:
    """``Long-Short-Term-Memory`` → ``Long Short Term Memory``.

    Casing is left alone: these slugs preserve the work's own capitalisation, so
    title-casing would corrupt acronyms (``GPT``, ``AI``) the way it would on
    ResearchGate. The cost is that an intra-word hyphen is lost
    (``Short-Term`` → ``Short Term``), which is harmless for a search query and
    barely visible in a card title.
    """
    text = " ".join((slug or "").replace("_", "-").replace("-", " ").split())
    return truncate_summary(text, _TITLE_MAX_LEN)


def parse_direct_mit_url(url: str) -> DirectMitArticle | str | None:
    """``DirectMitArticle`` for an article, the title for a book, else ``None``."""
    if not is_direct_mit_url(url):
        return None
    path = unquote(urlparse(url).path or "")

    match = _ARTICLE_PATH.match(path)
    if match:
        raw_slug = match.group("slug") or ""
        coordinates = {
            "journal": match.group("journal").lower(),
            "volume": match.group("volume"),
            "issue": match.group("issue"),
            "page": match.group("page"),
        }
        # An `article-pdf` URL ends in a filename, not a title slug — and that
        # filename is the DOI suffix verbatim, in both the legacy
        # (`neco.1997.9.8.1735.pdf`) and modern (`neco_a_01227.pdf`) forms. So
        # the dotted case is a *derivation*, not a search: cheaper than the
        # query below and still safe, because the same volume/issue/page check
        # rejects a suffix that turns out to name a different work.
        if "." in raw_slug:
            doi = doi_from_pdf_name(raw_slug)
            return DirectMitArticle(**coordinates, title="", doi=doi) if doi else None
        title = slug_title(raw_slug)
        if not title:
            return None
        return DirectMitArticle(**coordinates, title=title)

    match = _BOOK_PATH.match(path)
    if match:
        return slug_title(match.group("slug")) or None
    return None


def doi_from_pdf_name(name: str) -> str | None:
    """``neco.1997.9.8.1735.pdf`` → ``10.1162/neco.1997.9.8.1735``.

    The basename must carry a ``.`` or ``_`` of its own, which every real MIT
    Press suffix does and a generic ``paper.pdf`` does not — that check costs
    nothing and avoids a request for a DOI that cannot exist.
    """
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    stem = stem.strip("./")
    if not stem or not _PDF_STEM_RE.match(stem):
        return None
    return f"{_MIT_PRESS_PREFIX}/{stem}"


def citation_matches(item: dict, article: DirectMitArticle) -> bool:
    """True when a Crossref candidate agrees with the URL's own coordinates.

    Volume, issue and first page must all match. Crossref stores a page range
    (``1735-1780``) where the URL carries only the first page, so the range is
    compared on its opening value.
    """
    if str(item.get("volume") or "").strip() != article.volume:
        return False
    if str(item.get("issue") or "").strip().lower() != article.issue.lower():
        return False
    first_page = str(item.get("page") or "").strip().split("-")[0].strip()
    return bool(first_page) and first_page == article.page


def _slug_card(doc_id: int, url: str, title: str, kind: str, note: str) -> FetchResult:
    """Card from the URL alone — the fallback for both shapes.

    Never a fall-through to the generic GET: that is a guaranteed ``403`` on
    this host, which would leave the bookmark with no title at all. ``fetched``
    rather than a novel status, for ``search_url.py``'s reason — a new status
    silently drops the document out of orphan backfill.
    """
    return FetchResult(
        doc_id,
        url,
        "fetched",
        f"{title}\n\nMIT Press {kind}",
        None,
        note,
        title=title,
        card_summary=f"MIT Press {kind}: {title}.",
    )


async def _fetch_derived_doi(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
    article: DirectMitArticle,
) -> FetchResult | None:
    """Look up the DOI an ``article-pdf`` filename spells out.

    Returns ``None`` — a fall-through to the generic path, and so to this host's
    ``403`` — when the DOI does not resolve or resolves to a work whose
    coordinates disagree with the URL's. There is no slug card to fall back to
    here: this URL shape carries no title, so there is nothing honest to put on
    one.
    """
    assert article.doi is not None
    item, http_status, err = await fetch_crossref_work_item(client, article.doi)
    if item is None:
        log.debug("Derived DOI %s did not resolve for %s: %s", article.doi, url, err)
        return None
    if not citation_matches(item, article):
        log.debug("Derived DOI %s resolved to a different work for %s", article.doi, url)
        return None
    meta = _parse_doi_record(item)
    if meta is None:
        return None
    lookup = await enrich_abstract(client, meta, http_status)
    return doi_result(doc_id, url, lookup, via="direct.mit.edu pdf name")


async def _fetch_article(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
    article: DirectMitArticle,
) -> FetchResult | None:
    if article.doi:
        # No title to build a card from, so with lookups off there is nothing
        # this shape can offer and it falls through as it does today.
        if not cfg.doi_metadata_lookup:
            return None
        return await _fetch_derived_doi(client, doc_id, url, article)

    if not cfg.doi_metadata_lookup:
        return _slug_card(
            doc_id, url, article.title, "article", "direct.mit.edu; card built from url"
        )

    items, http_status, err = await fetch_crossref_bibliographic(
        client, article.title, prefix=_MIT_PRESS_PREFIX
    )
    if err:
        log.debug("Crossref bibliographic query failed for %s: %s", url, err)

    for item in items:
        if not citation_matches(item, article):
            continue
        meta = _parse_doi_record(item)
        if meta is None:
            continue
        lookup = await enrich_abstract(client, meta, http_status)
        return doi_result(doc_id, url, lookup, via="direct.mit.edu")

    # Ranked candidates that do not round-trip are discarded, not downgraded.
    return _slug_card(
        doc_id,
        url,
        article.title,
        "article",
        "direct.mit.edu citation unverified; card built from url",
    )


async def _fetch_book(doc_id: int, url: str, title: str) -> FetchResult:
    if not cfg.external_lookup_enabled:
        return _slug_card(doc_id, url, title, "book", "direct.mit.edu; card built from url")

    from pka.ingestion.openlibrary import lookup_by_title_author

    synopsis = await asyncio.to_thread(lookup_by_title_author, title)
    if synopsis is None:
        return _slug_card(
            doc_id,
            url,
            title,
            "book",
            "direct.mit.edu; no verified open library match, card built from url",
        )
    return FetchResult(
        doc_id,
        url,
        "fetched",
        "\n\n".join(part for part in (synopsis.title, synopsis.description) if part),
        None,
        "fetched via direct.mit.edu title → open library",
        title=synopsis.title or title,
        card_summary=truncate_summary(synopsis.description),
    )


async def fetch_direct_mit(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
) -> FetchResult | None:
    """Card for a ``direct.mit.edu`` article or book. ``None`` when neither."""
    parsed = parse_direct_mit_url(url)
    if parsed is None:
        return None
    if isinstance(parsed, DirectMitArticle):
        return await _fetch_article(client, doc_id, url, parsed)
    return await _fetch_book(doc_id, url, parsed)
