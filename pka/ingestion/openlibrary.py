"""Open Library lookups for book synopses (DESIGN.md §3.2).

Serves the book-cover image path and the Calibre runner. Unlike ``arxiv.py`` /
``biorxiv.py`` / ``wikipedia.py`` — which run inside the async fetch worker and
return a ``FetchResult`` — this module is called from *synchronous* ingestion
code (``image_pipeline.ingest_image``, ``runners/calibre``), so it exposes a
blocking API and keeps its own rate limiter rather than reusing the fetcher's
async one.

The resolution ladder (§3.2):

1. **ISBN**, checksum-validated first so a transposed digit costs no request.
   Self-verifying: an ISBN either resolves to an edition or it does not, so no
   match-confidence question arises.
2. **Title + author search**, with the canonical result *round-tripped* against
   what was extracted. Accepting rank 1 unverified is how you attach the wrong
   book's synopsis to a document, which is worse than attaching none — it shifts
   ``doc_embedding`` and makes the document findable under the wrong queries.

Every query is a pure function of ``(title, authors, isbn)``, never a
model-authored string, so results are cacheable by that key and the ladder is
replayable without re-running the VLM.

All entry points return ``None`` (never raise) when the lookup is disabled,
unavailable, or unverifiable — ingestion loops must not break on enrichment.
"""
from __future__ import annotations

import logging
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import httpx

from pka.config import settings as cfg
from pka.ingestion.chunker import trim_to_sentences
from pka.ingestion.rate_limit import SyncRateLimiter

log = logging.getLogger(__name__)

_ISBN_STRIP_RE = re.compile(r"[\s-]+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_ARTICLE_PREFIX_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)

# Open Library asks for ~1 req/s from unauthenticated clients.
_RATE_LIMIT_RPS = 1.0

_SEARCH_FIELDS = "key,title,author_name,first_publish_year"


@dataclass(frozen=True)
class BookSynopsis:
    """A resolved book description plus the identity it was resolved through."""

    title: str
    description: str
    authors: list[str] = field(default_factory=list)
    isbn: str | None = None
    work_key: str | None = None
    resolved_by: str = "isbn"  # isbn | search

    def embed_text(self, max_sentences: int | None = None) -> str:
        """Description trimmed for embedding.

        MiniLM truncates in the low hundreds of word-pieces, so a long synopsis
        silently loses its tail; §3.2 caps this at ``summary_max_sentences``.
        """
        limit = max_sentences if max_sentences is not None else cfg.summary_max_sentences
        return trim_to_sentences(self.description, limit)


# ── Rate limiting ─────────────────────────────────────────────────────────────

_limiter = SyncRateLimiter(rps=_RATE_LIMIT_RPS)


# Keyed by ISBN or (title, authors) — a shelf photo of ten books by one author
# should not issue the same author search ten times.
_cache: dict[str, BookSynopsis | None] = {}
_cache_lock = threading.Lock()


def reset_cache() -> None:
    """Drop the in-process lookup cache — used by the test suite."""
    with _cache_lock:
        _cache.clear()


# ── ISBN validation ───────────────────────────────────────────────────────────

def normalize_isbn(raw: object) -> str | None:
    """Strip separators and return a 10- or 13-character ISBN, or ``None``."""
    if raw is None:
        return None
    value = _ISBN_STRIP_RE.sub("", str(raw).strip()).upper()
    if len(value) == 10:
        if not value[:9].isdigit() or (not value[9].isdigit() and value[9] != "X"):
            return None
        return value
    if len(value) == 13:
        return value if value.isdigit() else None
    return None


def isbn_checksum_valid(raw: object) -> bool:
    """Validate the ISBN check digit.

    A single transposed or mistyped digit is the common OCR/VLM failure on a
    printed ISBN, and the check digit catches it for free — so a bad read costs
    no network request and drops cleanly to the title+author rung.
    """
    isbn = normalize_isbn(raw)
    if isbn is None:
        return False
    if len(isbn) == 10:
        total = 0
        for i, char in enumerate(isbn):
            val = 10 if char == "X" else int(char)
            total += (10 - i) * val
        return total % 11 == 0
    total = 0
    for i, char in enumerate(isbn):
        total += int(char) * (1 if i % 2 == 0 else 3)
    return total % 10 == 0


# ── Title matching (round-trip verification) ──────────────────────────────────

def _fold_accents(value: str) -> str:
    """Strip diacritics so "Gödel" and "Godel" compare equal.

    A cover reading and a catalogue record routinely disagree on accents, and
    without this the non-alphanumeric fold turns "Gödel" into "g del" — a
    guaranteed mismatch on exactly the titles most likely to need one.
    """
    decomposed = unicodedata.normalize("NFKD", str(value))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _normalize_title(value: str) -> str:
    text = _ARTICLE_PREFIX_RE.sub("", _fold_accents(value).strip())
    return _NON_ALNUM_RE.sub(" ", text.lower()).strip()


def titles_match(extracted: str, canonical: str) -> bool:
    """True when two titles are the same work.

    Containment counts in both directions: a cover shows "Dune" where the
    catalogue holds "Dune: Book One", and vice versa. Both sides must be
    non-trivial — a two-character extraction must not match everything.
    """
    a, b = _normalize_title(extracted), _normalize_title(canonical)
    if not a or not b or min(len(a), len(b)) < 3:
        return False
    return a == b or a in b or b in a


def _surnames(authors: list[str]) -> set[str]:
    out: set[str] = set()
    for name in authors:
        parts = _NON_ALNUM_RE.sub(" ", _fold_accents(name).lower()).split()
        if parts:
            out.add(parts[-1])
    return out


def authors_match(extracted: list[str], canonical: list[str]) -> bool:
    """True when at least one surname is shared, or nothing was extracted.

    Absent extracted authors is not a mismatch — a spine-only bookshelf photo
    often yields a title alone, and title containment already carries the match.
    """
    wanted = _surnames(extracted)
    if not wanted:
        return True
    return bool(wanted & _surnames(canonical))


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _get_json(path: str, params: dict[str, str] | None = None) -> Any | None:
    """GET a JSON document from Open Library. ``None`` on any failure."""
    url = f"{cfg.openlibrary_base_url.rstrip('/')}{path}"
    _limiter.wait(url)
    try:
        resp = httpx.get(
            url,
            params=params,
            follow_redirects=True,
            timeout=cfg.fetch_timeout_seconds,
            headers={"User-Agent": cfg.fetch_user_agent},
        )
    except httpx.HTTPError as exc:
        log.debug("Open Library request failed (%s): %s", url, exc)
        return None
    if resp.status_code >= 400:
        log.debug("Open Library %s returned HTTP %d", path, resp.status_code)
        return None
    try:
        return resp.json()
    except ValueError:
        log.debug("Open Library %s returned non-JSON", path)
        return None


def _description_text(record: Any) -> str:
    """Pull a description out of an edition or work record.

    Open Library returns either a bare string or ``{"type": ..., "value": ...}``
    depending on the record's vintage.
    """
    if not isinstance(record, dict):
        return ""
    raw = record.get("description")
    if isinstance(raw, dict):
        raw = raw.get("value")
    return str(raw).strip() if raw else ""


# ── Lookup rungs ──────────────────────────────────────────────────────────────

def lookup_by_isbn(isbn: object) -> BookSynopsis | None:
    """Resolve an edition by ISBN, then its work, for a description."""
    normalized = normalize_isbn(isbn)
    if normalized is None or not isbn_checksum_valid(normalized):
        log.debug("Rejecting ISBN before lookup: %r", isbn)
        return None

    edition = _get_json(f"/isbn/{normalized}.json")
    if not isinstance(edition, dict):
        return None

    title = str(edition.get("title") or "").strip()
    description = _description_text(edition)
    work_key: str | None = None

    works = edition.get("works")
    if isinstance(works, list) and works and isinstance(works[0], dict):
        work_key = str(works[0].get("key") or "") or None
    if work_key and not description:
        work = _get_json(f"{work_key}.json")
        description = _description_text(work)
        if isinstance(work, dict) and not title:
            title = str(work.get("title") or "").strip()

    if not description:
        log.debug("Open Library has no description for ISBN %s", normalized)
        return None
    return BookSynopsis(
        title=title,
        description=description,
        isbn=normalized,
        work_key=work_key,
        resolved_by="isbn",
    )


def lookup_by_title_author(title: str, authors: list[str] | None = None) -> BookSynopsis | None:
    """Search by title (+author), accepting a hit only if it round-trips."""
    query_title = (title or "").strip()
    if len(_normalize_title(query_title)) < 3:
        log.debug("Title too thin to search: %r", title)
        return None

    author_list = [a for a in (authors or []) if str(a).strip()]
    params = {"title": query_title, "limit": "5", "fields": _SEARCH_FIELDS}
    if author_list:
        params["author"] = ", ".join(author_list)

    payload = _get_json("/search.json", params=params)
    docs = payload.get("docs") if isinstance(payload, dict) else None
    if not isinstance(docs, list):
        return None

    for doc in docs:
        if not isinstance(doc, dict):
            continue
        canonical_title = str(doc.get("title") or "").strip()
        canonical_authors = [
            str(a) for a in (doc.get("author_name") or []) if str(a).strip()
        ]
        if not titles_match(query_title, canonical_title):
            continue
        if not authors_match(author_list, canonical_authors):
            continue

        work_key = str(doc.get("key") or "") or None
        if not work_key:
            continue
        description = _description_text(_get_json(f"{work_key}.json"))
        if not description:
            continue
        return BookSynopsis(
            title=canonical_title,
            description=description,
            authors=canonical_authors,
            work_key=work_key,
            resolved_by="search",
        )

    log.debug("No verified Open Library match for %r", query_title)
    return None


def _cache_key(title: str, authors: list[str], isbn: str | None) -> str:
    if isbn:
        return f"isbn:{isbn}"
    return f"ta:{_normalize_title(title)}|{','.join(sorted(_surnames(authors)))}"


def lookup_book(
    title: str = "",
    authors: list[str] | None = None,
    isbn: object = None,
) -> BookSynopsis | None:
    """Run the §3.2 ladder: ISBN, verified title+author search, then a second catalogue.

    Returns ``None`` when ``external_lookup_enabled`` is off — this is the single
    enforcement point for the network boundary, so callers need no flag check.
    """
    if not cfg.external_lookup_enabled:
        return None

    author_list = [a for a in (authors or []) if str(a).strip()]
    normalized_isbn = normalize_isbn(isbn)
    if normalized_isbn and not isbn_checksum_valid(normalized_isbn):
        # Bad check digit: skip the ISBN rung entirely rather than spend a request.
        log.debug("ISBN %s failed checksum; falling back to title/author", normalized_isbn)
        normalized_isbn = None

    key = _cache_key(title, author_list, normalized_isbn)
    with _cache_lock:
        if key in _cache:
            return _cache[key]

    result: BookSynopsis | None = None
    if normalized_isbn:
        result = lookup_by_isbn(normalized_isbn)
    if result is None and title:
        result = lookup_by_title_author(title, author_list)
    if result is None and title:
        # Third rung: a second catalogue, for books Open Library does not hold.
        # Imported here rather than at module scope because that module imports
        # from this one — the same lazy-import shape as fetcher/arxiv.
        from pka.ingestion.book_search import search_synopsis

        result = search_synopsis(title, author_list)

    with _cache_lock:
        _cache[key] = result
    return result
