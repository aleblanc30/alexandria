"""Third rung of the §3.2 book-synopsis ladder: a second catalogue.

Reached only when Open Library does not know a book — self-published work,
foreign editions, and the theses and reports the cover prompt also files under
the book labels. Default off, behind ``cover_search_fallback`` *and*
``external_lookup_enabled`` (see ``Settings.cover_search_active``): a fallback
must never be the thing that first opens a network path.

**Why a catalogue and not a general web search.** The job of this rung is to turn
``(title, authors)`` into a description. A web search engine answers that with
retailer and review pages that then have to be scraped and trusted; a book
catalogue answers it directly, with canonical title and author fields that can be
round-tripped exactly as the Open Library rung is. Google Books is the default
because it is documented, free, and needs no key or signup to start — a
default-off feature should be switch-on-and-try, not switch-on-and-register.

**The seam.** A provider is any ``(title, authors) -> BookSynopsis | None``
callable registered in :data:`_PROVIDERS` under the name used by
``search_provider``. Swapping in a real web-search backend (Brave, Tavily) means
adding one function and one registry entry; nothing in the cascade changes.
Providers must never raise — :func:`search_synopsis` is called from ingestion.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx

from pka.config import settings as cfg
from pka.ingestion.openlibrary import (
    BookSynopsis,
    _SyncRateLimiter,
    authors_match,
    titles_match,
)

log = logging.getLogger(__name__)

GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"

# Keyless use is quota-limited per IP, so stay polite even though the free tier
# does not demand it.
_limiter = _SyncRateLimiter(rps=1.0)

SearchProvider = Callable[[str, list[str]], "BookSynopsis | None"]


def _google_books_search(title: str, authors: list[str]) -> BookSynopsis | None:
    """Resolve a description from Google Books, verified the same way as §3.2 rung 2."""
    terms = [f'intitle:"{title}"']
    if authors:
        terms.append(f'inauthor:"{authors[0]}"')
    params: dict[str, Any] = {"q": " ".join(terms), "maxResults": 5}
    if cfg.google_books_api_key:
        params["key"] = cfg.google_books_api_key

    _limiter.wait()
    try:
        resp = httpx.get(
            GOOGLE_BOOKS_URL,
            params=params,
            follow_redirects=True,
            timeout=cfg.fetch_timeout_seconds,
            headers={"User-Agent": cfg.fetch_user_agent},
        )
    except httpx.HTTPError as exc:
        log.debug("Google Books request failed: %s", exc)
        return None
    if resp.status_code >= 400:
        log.debug("Google Books returned HTTP %d", resp.status_code)
        return None
    try:
        payload = resp.json()
    except ValueError:
        log.debug("Google Books returned non-JSON")
        return None

    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None

    for item in items:
        info = item.get("volumeInfo") if isinstance(item, dict) else None
        if not isinstance(info, dict):
            continue
        canonical_title = str(info.get("title") or "").strip()
        canonical_authors = [str(a) for a in (info.get("authors") or []) if str(a).strip()]
        description = str(info.get("description") or "").strip()
        if not description:
            continue
        # Same discipline as the Open Library rung: trust agreement, not ranking.
        if not titles_match(title, canonical_title):
            continue
        if not authors_match(authors, canonical_authors):
            continue
        return BookSynopsis(
            title=canonical_title,
            description=description,
            authors=canonical_authors,
            resolved_by="google_books",
        )

    log.debug("No verified Google Books match for %r", title)
    return None


BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


def _verify_web_result(
    title: str,
    authors: list[str],
    result_title: str,
    snippet: str,
) -> bool:
    """Verification for a *web* result, which is weaker than a catalogue record.

    A catalogue returns a canonical title field; a search engine returns a page
    title like "Dune by Frank Herbert | Goodreads". So the check is one-directional
    — the extracted title must appear in the result title, not the reverse — and
    an extracted author must show up somewhere in the title or snippet. Looser
    than :func:`titles_match` by necessity, which is exactly why this rung is
    ordered last.
    """
    from pka.ingestion.openlibrary import _normalize_title, _surnames

    wanted = _normalize_title(title)
    if len(wanted) < 3 or wanted not in _normalize_title(result_title):
        return False
    surnames = _surnames(authors)
    if not surnames:
        return True
    haystack = _normalize_title(f"{result_title} {snippet}")
    return any(s in haystack for s in surnames)


def _brave_search(title: str, authors: list[str]) -> BookSynopsis | None:
    """Brave Search web results, verified loosely and used as a last resort.

    Needs ``SECRET_ALEXANDRIA_SEARCH_API_KEY``; without one this rung skips
    rather than erroring, so listing it in the chain is harmless until a key
    exists. The artifact is a search snippet — thinner and less reliable than a
    catalogue synopsis, and marked ``resolved_by="brave"`` so it stays auditable
    and can be purged separately if it proves noisy.
    """
    if not cfg.search_api_key:
        log.debug("Brave rung skipped: no search_api_key configured")
        return None

    query = " ".join([f'"{title}"', *(f'"{a}"' for a in authors[:1]), "book"])
    _limiter.wait()
    try:
        resp = httpx.get(
            BRAVE_SEARCH_URL,
            params={"q": query, "count": 5},
            follow_redirects=True,
            timeout=cfg.fetch_timeout_seconds,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": cfg.search_api_key,
                "User-Agent": cfg.fetch_user_agent,
            },
        )
    except httpx.HTTPError as exc:
        log.debug("Brave request failed: %s", exc)
        return None
    if resp.status_code >= 400:
        log.debug("Brave returned HTTP %d", resp.status_code)
        return None
    try:
        payload = resp.json()
    except ValueError:
        log.debug("Brave returned non-JSON")
        return None

    web = payload.get("web") if isinstance(payload, dict) else None
    results = web.get("results") if isinstance(web, dict) else None
    if not isinstance(results, list):
        return None

    for item in results:
        if not isinstance(item, dict):
            continue
        result_title = str(item.get("title") or "").strip()
        snippet = str(item.get("description") or "").strip()
        if not snippet:
            continue
        if not _verify_web_result(title, authors, result_title, snippet):
            continue
        return BookSynopsis(
            title=title,
            description=snippet,
            authors=authors,
            resolved_by="brave",
        )

    log.debug("No verified Brave match for %r", title)
    return None


_PROVIDERS: dict[str, SearchProvider] = {
    "google_books": _google_books_search,
    "brave": _brave_search,
}


def _provider_chain() -> list[str]:
    """Providers to try, in order, from the comma-separated ``search_provider``.

    A chain rather than a single choice so a weaker rung can run *after* a
    stronger one instead of replacing it: ``google_books,brave`` consults the
    catalogue first and only falls to web snippets when it misses.
    """
    raw = (cfg.search_provider or "google_books").strip()
    return [name.strip() for name in raw.split(",") if name.strip()]


def search_synopsis(title: str, authors: list[str] | None = None) -> BookSynopsis | None:
    """Run the configured search provider. ``None`` when off, unknown, or unverified.

    Providers are tried in ``search_provider`` order; the first verified hit
    wins. ``cover_search_active`` — not ``cover_search_fallback`` — is the gate,
    so the fallback flag genuinely has no effect without the lookup flag it falls
    back from (§1.1, no implicit escalation).
    """
    if not cfg.cover_search_active:
        return None
    if not (title or "").strip():
        return None

    author_list = [a for a in (authors or []) if str(a).strip()]
    for name in _provider_chain():
        provider = _PROVIDERS.get(name)
        if provider is None:
            log.warning(
                "Unknown search provider %r; known: %s",
                name, ", ".join(sorted(_PROVIDERS)),
            )
            continue
        try:
            found = provider(title, author_list)
        except Exception as exc:
            log.warning("Search provider %r failed: %s", name, exc)
            continue
        if found is not None:
            return found
    return None
