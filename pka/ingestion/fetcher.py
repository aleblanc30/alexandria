"""
Async URL fetcher for Firefox bookmark content.
Runs as a background job — does not block the ingestion pipeline.

Strategy:
  - httpx AsyncClient with rate limiting (token bucket, per-domain)
  - trafilatura for main-content extraction; readability-lxml as fallback
  - Remote PDFs: download bytes, extract via book_extractor.extract_pdf_report, embed
  - Scanned PDFs (readable, paginated, no text layer) → "no_text_layer", never re-fetched
  - Other non-HTML targets (EPUB, torrents, …) flagged as "skipped"
  - Local ``file:`` URLs and bare filesystem paths → "unfetchable" (no HTTP fetch)
  - Extracted text that is a consent wall, a bot check or a bare stylesheet →
    "unfetchable" with the wall named, never stored (``content_gate.py``)
  - Auth failures, timeouts, 4xx/5xx → status "unfetchable", logged to fetch_log
  - HTTP 404 with ``fetch_wayback_fallback`` enabled → query archive.org for a snapshot
  - ``*.wikipedia.org`` URLs → MediaWiki Action API (with retries) instead of HTML scrape
  - Amazon book product pages → title + editorial summary extracted for browse cards
  - YouTube video URLs → oEmbed API (title + channel, no API key); title on cards
  - YouTube channel / playlist URLs → card from the URL, no request (a scrape returns
    Google's consent interstitial, which then reads as the document)
  - Reddit thread URLs → public ``.json`` listing (title + selftext, or top comments
    for a link post); URL-derived title/subreddit fallback when blocked
  - ``arxiv.org`` URLs → export.arxiv.org API (metadata + PDF); title and abstract on cards
  - ``biorxiv.org`` URLs → api.biorxiv.org DOI lookup (metadata + PDF); title and abstract on cards
  - ``pubmed.ncbi.nlm.nih.gov`` URLs → NCBI efetch (metadata + abstract, no PDF); title and abstract on cards
  - ``researchgate.net/publication/…`` URLs → card from the URL slug, no request (hard-blocked)
  - ``mitpress.mit.edu`` book pages → ISBN from the path; Open Library synopsis when
    ``external_lookup_enabled``, otherwise a slug card — no request either way
  - ``direct.mit.edu`` → hard 403, so articles resolve by Crossref bibliographic query
    **verified against the URL's volume/issue/page**, and books by Open Library title;
    either falls back to a slug card, never to the blocked GET
  - ``doi.org`` URLs → DOI content negotiation (CSL-JSON) instead of scraping the redirect target
  - ``nature.com`` / ``link.springer.com`` / ``journals.aps.org`` / ``sciencedirect.com`` →
    DOI derived from the URL, resolved via Crossref (+ Semantic Scholar for a missing
    abstract); gate: ``doi_metadata_lookup``. Replaces a paywall scrape or a 403.

Previously skipped PDF bookmarks stay ``fetch_status=skipped`` until reset manually, e.g.::

    UPDATE documents SET fetch_status = 'pending'
    WHERE source = 'firefox' AND fetch_status = 'skipped' AND url_or_path LIKE '%.pdf';
"""

import asyncio
import heapq
import logging
import re
import time
from collections import deque
from collections.abc import Callable, Iterable
from urllib.parse import urlparse

import httpx
import sqlalchemy as sa

from pka.config import settings as cfg
from pka.constants import FetchStatus, Source
from pka.db.queries import get_engine
from pka.db.schema import documents, fetch_log
from pka.ingestion.content_gate import interstitial_reason
from pka.ingestion.fetch_base import (  # re-exported: shared primitives live one layer down
    _HTML_TYPES,
    FetchResult,
    _extract_text,
    _fetch_pdf_result,
    _http_timeout,
    _is_pdf_bytes,
    _is_pdf_content_type,
    _limiter,
    _url_looks_like_pdf,
)
from pka.ingestion.progress import advance, should_stop
from pka.ingestion.rate_limit import SlotScheduler, domain_of

log = logging.getLogger(__name__)

# Extensions that are not HTML and should be skipped (PDF handled separately)
_SKIP_EXTENSIONS = {".epub", ".torrent", ".zip", ".gz", ".mp4", ".mp3"}

# Windows drive letters (C:, C\:, C:/…) and UNC paths — not HTTP fetch targets
_LOCAL_PATH_RE = re.compile(r"^[A-Za-z](?:[/\\])?:", re.ASCII)
_UNC_PATH_RE = re.compile(r"^\\\\")


def _looks_like_local_path(value: str) -> bool:
    s = value.strip()
    if not s:
        return False
    if _UNC_PATH_RE.match(s):
        return True
    if _LOCAL_PATH_RE.match(s):
        return True
    # Absolute unix path without a scheme (file:///… is handled via scheme=file).
    return s.startswith("/") and not s.startswith("//")


def bookmark_url_unfetchable_reason(url: str) -> str | None:
    """Return an error reason when a bookmark cannot be fetched over HTTP(S)."""
    raw = (url or "").strip()
    if not raw:
        return "empty url"
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    if scheme in ("http", "https"):
        return None
    if scheme == "file":
        return "local file url"
    if len(scheme) == 1 and scheme.isalpha():
        return "local file path"
    if scheme:
        return f"unsupported url scheme: {scheme}"
    candidate = parsed.path or raw
    if _looks_like_local_path(candidate) or _looks_like_local_path(raw):
        return "local file path"
    return "missing http(s) scheme"


def _fetch_budget_seconds(
    *,
    pdf: bool = False,
    wayback: bool = False,
    wikipedia: bool = False,
    preprint: bool = False,
    doi: bool = False,
    pii: bool = False,
    book_lookup: bool = False,
) -> float:
    """Hard ceiling per URL, covering the request(s) and text extraction.

    The pool's rate-limit wait happens *before* this budget starts (the worker
    sleeps on the delay ``_DomainQueue.get`` hands back), so a backed-up domain
    no longer eats into a healthy site's request budget. A fetch that claims its
    own slot inside ``_fetch_one_impl`` — the ``slot_held=False`` fall-through,
    and every per-site handler — still waits on the limiter inside the budget.
    """
    if pdf:
        base = (
            cfg.fetch_pdf_timeout_seconds
            + cfg.fetch_connect_timeout_seconds
            + cfg.fetch_pdf_budget_extra_seconds
        )
    else:
        base = cfg.fetch_timeout_seconds + cfg.fetch_connect_timeout_seconds + 5.0
    if wayback and cfg.fetch_wayback_fallback:
        base += cfg.fetch_wayback_extra_budget_seconds
    if wikipedia:
        attempts = cfg.fetch_wikipedia_max_retries + 1
        base += attempts * (cfg.fetch_timeout_seconds + cfg.fetch_connect_timeout_seconds)
        base += cfg.fetch_wikipedia_max_retries * cfg.fetch_wikipedia_retry_delay_seconds
    if preprint:
        base += cfg.fetch_timeout_seconds + cfg.fetch_connect_timeout_seconds
        base += cfg.fetch_pdf_timeout_seconds + cfg.fetch_pdf_budget_extra_seconds
    if doi:
        # Worst case is two sequential requests and no PDF: the primary record,
        # then the Semantic Scholar rung when it carried no abstract.
        base += cfg.fetch_timeout_seconds + cfg.fetch_connect_timeout_seconds
    if pii:
        # ScienceDirect adds the alternative-id query in front of those two.
        base += 2 * (cfg.fetch_timeout_seconds + cfg.fetch_connect_timeout_seconds)
    if book_lookup and cfg.external_lookup_enabled:
        # openlibrary.py is synchronous and makes up to two requests of its own
        # (edition or search, then work) at fetch_timeout_seconds each, behind a
        # 1 rps limiter — none of which the base budget accounts for.
        base += 2 * (cfg.fetch_timeout_seconds + cfg.fetch_connect_timeout_seconds)
    return base


# ── Dispatch: which domain's slot a worker must hold ─────────────────────────


def _throttle_key(url: str) -> str | None:
    """The domain whose send slot a worker must hold before starting ``url``.

    ``None`` means "do not order this item by a slot": either no request is made
    at all (local path, bad scheme, skipped extension, the two card-from-URL
    handlers), or the request is made by a per-site handler that claims its own
    slots against its own hosts (one arXiv item claims ``export.arxiv.org`` and
    then ``arxiv.org``, which a single worker-level claim cannot represent).

    This mirrors the guards and the handler dispatch at the top of
    ``_fetch_one_impl`` and drifts from them if they change. The drift is not
    symmetric: a key returned where ``_fetch_one_impl`` returns early only wastes
    a slot, while a *missing* key merely leaves the item unordered — the
    ``slot_held=False`` path still claims before sending, so the rate limit holds
    either way.

    Blind spot it shares with the limiter itself: the key is the host in the
    *bookmarked* URL, so a redirect is spaced against the host that redirected
    rather than the one that answers (see
    ``planning/archive/PUBLISHER_FETCH_HANDLERS.md``).
    """
    from pka.ingestion.aps import parse_aps_url
    from pka.ingestion.arxiv import parse_arxiv_url
    from pka.ingestion.biorxiv import parse_biorxiv_url
    from pka.ingestion.direct_mit import parse_direct_mit_url
    from pka.ingestion.doi_org import parse_doi_url
    from pka.ingestion.mitpress import parse_mitpress_url
    from pka.ingestion.nature import parse_nature_url
    from pka.ingestion.pubmed import parse_pubmed_url
    from pka.ingestion.reddit_bookmark import parse_reddit_permalink
    from pka.ingestion.researchgate import parse_researchgate_url
    from pka.ingestion.sciencedirect import parse_sciencedirect_url
    from pka.ingestion.search_url import parse_search_url
    from pka.ingestion.springer import parse_springer_url
    from pka.ingestion.wikipedia import is_wikipedia_special, parse_wikipedia_url
    from pka.ingestion.youtube_bookmark import parse_youtube_page_url, parse_youtube_url

    if bookmark_url_unfetchable_reason(url):
        return None
    if is_wikipedia_special(url) or parse_wikipedia_url(url) is not None:
        return None
    if parse_search_url(url) is not None or parse_researchgate_url(url) is not None:
        return None
    if parse_youtube_page_url(url) is not None:
        return None
    for parse in (
        parse_youtube_url,
        parse_reddit_permalink,
        parse_arxiv_url,
        parse_biorxiv_url,
        parse_pubmed_url,
        parse_mitpress_url,
        parse_direct_mit_url,
        parse_doi_url,
        parse_nature_url,
        parse_springer_url,
        parse_aps_url,
        parse_sciencedirect_url,
    ):
        if parse(url) is not None:
            return None
    if not _url_looks_like_pdf(url):
        path = urlparse(url).path.lower()
        if any(path.endswith(ext) for ext in _SKIP_EXTENSIONS):
            return None
    return domain_of(url)


class _DomainQueue:
    """Work queue that hands out whatever a worker can actually send *now*.

    A flat FIFO parks every worker on one busy host: the throttle is applied
    after the choice of URL, so a worker that draws a URL from a domain a second
    from its next slot sleeps for that second while holding a worker slot, with
    ready work from other domains queued behind it.

    ``get`` picks, in order: a throttled item whose slot is open now; else an
    item needing no slot; else the throttled item whose slot opens soonest,
    together with the delay to sleep first.

    **``get`` is synchronous by design.** Choosing a bucket and claiming its slot
    happen with no ``await`` between them, which is what stops two workers from
    both reading a domain as ready and then stacking up. A peek-then-claim split
    reintroduces that race and must not be "simplified" back in.

    Not solved here: a batch made up entirely of one site's per-site handler
    (every URL a Wikipedia one, say) carries no throttle key at all, so nothing
    is ordered and the workers converge on that handler's own limiter.
    """

    def __init__(
        self,
        items: Iterable[tuple[int, str]] = (),
        *,
        scheduler: SlotScheduler | None = None,
    ) -> None:
        self._scheduler = scheduler if scheduler is not None else _limiter.scheduler
        self._free: deque[tuple[int, str]] = deque()
        self._by_key: dict[str, deque[tuple[int, str]]] = {}
        # Exactly one entry per non-empty bucket, keyed by that domain's next
        # slot; entries go stale as slots are claimed and are refreshed on pop.
        self._heap: list[tuple[float, str]] = []
        self._size = 0
        for item in items:
            self.put(item)

    def __len__(self) -> int:
        return self._size

    def put(self, item: tuple[int, str]) -> None:
        key = _throttle_key(item[1])
        self._size += 1
        if key is None:
            self._free.append(item)
            return
        bucket = self._by_key.get(key)
        if bucket is None:
            bucket = self._by_key[key] = deque()
            heapq.heappush(self._heap, (self._scheduler.next_slot(key), key))
        bucket.append(item)

    def _pop_soonest(self) -> tuple[str, float] | None:
        """Remove and return the (key, next_slot) of the earliest-ready bucket."""
        while self._heap:
            slot, key = heapq.heappop(self._heap)
            if not self._by_key.get(key):
                self._by_key.pop(key, None)
                continue
            fresh = self._scheduler.next_slot(key)
            if fresh > slot:  # stale: slots were claimed since this was pushed
                heapq.heappush(self._heap, (fresh, key))
                continue
            return key, fresh
        return None

    def get(self) -> tuple[tuple[int, str], float, bool] | None:
        """``(item, delay, slot_held)``, or ``None`` when the queue is empty.

        ``delay`` is how long the worker must sleep before its first request;
        ``slot_held`` says the slot for that request is already claimed, so
        ``_fetch_one`` must not claim a second one.
        """
        if self._size == 0:
            return None
        head = self._pop_soonest()
        if head is not None:
            key, slot = head
            if slot <= self._scheduler.now() or not self._free:
                bucket = self._by_key[key]
                item = bucket.popleft()
                delay = self._scheduler.claim(key)
                if bucket:
                    heapq.heappush(self._heap, (self._scheduler.next_slot(key), key))
                else:
                    del self._by_key[key]
                self._size -= 1
                return item, delay, True
            # Throttled work exists but is not ready, and slot-free work is
            # waiting: fill the gap with that instead of sleeping on this.
            heapq.heappush(self._heap, (slot, key))
        item = self._free.popleft()
        self._size -= 1
        return item, 0.0, False


# ── Per-URL fetch ─────────────────────────────────────────────────────────────


async def _fetch_one_impl(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
    *,
    slot_held: bool = False,
) -> FetchResult:
    from pka.ingestion.wikipedia import (
        fetch_wikipedia_with_retries,
        is_wikipedia_special,
        parse_wikipedia_url,
    )

    if reason := bookmark_url_unfetchable_reason(url):
        return FetchResult(doc_id, url, "unfetchable", None, None, reason)

    from pka.ingestion.search_url import search_url_result

    if (result := search_url_result(doc_id, url)) is not None:
        return result

    from pka.ingestion.researchgate import researchgate_result

    # Sync and un-awaited like search_url_result above: no request is made, so
    # there is no client, no rate-limiter slot and no budget leg.
    if (result := researchgate_result(doc_id, url)) is not None:
        return result

    if is_wikipedia_special(url):
        return FetchResult(doc_id, url, "skipped", None, None, "wikipedia special page")
    if parse_wikipedia_url(url) is not None:
        return await fetch_wikipedia_with_retries(client, doc_id, url)

    from pka.ingestion.youtube_bookmark import (
        fetch_youtube_video,
        parse_youtube_url,
        youtube_page_result,
    )

    if parse_youtube_url(url):
        result = await fetch_youtube_video(client, doc_id, url)
        if result is not None:
            return result

    # Sync and un-awaited: a channel or playlist card is built from the URL, and
    # scraping one returns Google's consent interstitial, not page content.
    if (result := youtube_page_result(doc_id, url)) is not None:
        return result

    from pka.ingestion.reddit_bookmark import fetch_reddit_thread, parse_reddit_permalink

    if parse_reddit_permalink(url):
        result = await fetch_reddit_thread(client, doc_id, url)
        if result is not None:
            return result

    from pka.ingestion.arxiv import fetch_arxiv_paper, parse_arxiv_url

    if parse_arxiv_url(url):
        result = await fetch_arxiv_paper(client, doc_id, url)
        if result is not None:
            return result

    from pka.ingestion.biorxiv import fetch_biorxiv_paper, parse_biorxiv_url

    if parse_biorxiv_url(url):
        result = await fetch_biorxiv_paper(client, doc_id, url)
        if result is not None:
            return result

    from pka.ingestion.pubmed import fetch_pubmed_article, parse_pubmed_url

    if parse_pubmed_url(url):
        result = await fetch_pubmed_article(client, doc_id, url)
        if result is not None:
            return result

    # Identifier-carrying publisher URLs (PUBLISHER_FETCH_HANDLERS.md). Order
    # within this block is free — the host checks are disjoint — but it must
    # stay after arXiv: doi.org/10.48550/arXiv.… is a valid arXiv DOI, and
    # doi_org.py hands that cross-walk back to fetch_arxiv_paper itself.
    from pka.ingestion.mitpress import fetch_mitpress_book, parse_mitpress_url

    if parse_mitpress_url(url):
        result = await fetch_mitpress_book(client, doc_id, url)
        if result is not None:
            return result

    from pka.ingestion.direct_mit import fetch_direct_mit, parse_direct_mit_url

    if parse_direct_mit_url(url) is not None:
        result = await fetch_direct_mit(client, doc_id, url)
        if result is not None:
            return result

    from pka.ingestion.doi_org import fetch_doi_url, parse_doi_url

    if parse_doi_url(url):
        result = await fetch_doi_url(client, doc_id, url)
        if result is not None:
            return result

    from pka.ingestion.nature import fetch_nature_article, parse_nature_url

    if parse_nature_url(url):
        result = await fetch_nature_article(client, doc_id, url)
        if result is not None:
            return result

    from pka.ingestion.springer import fetch_springer_article, parse_springer_url

    if parse_springer_url(url):
        result = await fetch_springer_article(client, doc_id, url)
        if result is not None:
            return result

    from pka.ingestion.aps import fetch_aps_article, parse_aps_url

    if parse_aps_url(url):
        result = await fetch_aps_article(client, doc_id, url)
        if result is not None:
            return result

    from pka.ingestion.sciencedirect import (
        fetch_sciencedirect_article,
        parse_sciencedirect_url,
    )

    if parse_sciencedirect_url(url):
        result = await fetch_sciencedirect_article(client, doc_id, url)
        if result is not None:
            return result

    expect_pdf = _url_looks_like_pdf(url)
    path = urlparse(url).path.lower()
    if not expect_pdf and any(path.endswith(ext) for ext in _SKIP_EXTENSIONS):
        return FetchResult(doc_id, url, "skipped", None, None, "non-html extension")

    # The pool claims this slot before dispatching (``_DomainQueue``), so
    # claiming again here would spend two slots per URL and halve the rate.
    # Still needed for every other caller, and for the fall-through above where
    # a per-site handler declined and this plain GET was not what was scheduled.
    if not slot_held:
        await _limiter.wait(url)

    try:
        resp = await client.get(
            url,
            follow_redirects=True,
            timeout=_http_timeout(pdf=expect_pdf),
        )
    except httpx.TimeoutException:
        return FetchResult(doc_id, url, "unfetchable", None, None, "timeout")
    except httpx.RequestError as exc:
        return FetchResult(doc_id, url, "unfetchable", None, None, str(exc))

    http_status = resp.status_code

    if http_status == 404 and cfg.fetch_wayback_fallback:
        from pka.ingestion.wayback import fetch_via_wayback

        wayback = await fetch_via_wayback(client, doc_id, url)
        if wayback and wayback.status == "fetched":
            return wayback

    if http_status >= 400:
        reason = f"HTTP {http_status}"
        return FetchResult(doc_id, url, "unfetchable", None, http_status, reason)

    content_type = resp.headers.get("content-type", "").split(";")[0].strip()
    body = resp.content

    if expect_pdf or _is_pdf_content_type(content_type) or _is_pdf_bytes(body):
        return await asyncio.to_thread(_fetch_pdf_result, doc_id, url, body, http_status)

    if content_type and content_type not in _HTML_TYPES:
        return FetchResult(
            doc_id, url, "skipped", None, http_status, f"non-html content-type: {content_type}"
        )

    from pka.ingestion.amazon import extract_amazon_book, is_amazon_book_url

    if is_amazon_book_url(url):
        book = await asyncio.to_thread(extract_amazon_book, resp.text)
        if book:
            return FetchResult(
                doc_id,
                url,
                "fetched",
                book.summary,
                http_status,
                "fetched via amazon book handler",
                title=book.title,
            )

    text = await asyncio.to_thread(_extract_text, resp.text, url)
    if not text:
        return FetchResult(
            doc_id, url, "unfetchable", None, http_status, "content extraction yielded no text"
        )

    # A consent wall or a stylesheet extracts as cleanly as an article does, so
    # nothing above this rejects it. Recording it unfetchable — rather than
    # storing it — keeps meaningless text out of the chunks and the vector
    # store, and puts the domain in the unfetchable lists, where a missing
    # handler is something the operator can see and act on.
    if reason := interstitial_reason(text):
        return FetchResult(doc_id, url, "unfetchable", None, http_status, reason)

    return FetchResult(doc_id, url, "fetched", text, http_status, None)


async def _fetch_one(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
    *,
    slot_held: bool = False,
) -> FetchResult:
    from pka.ingestion.aps import parse_aps_url
    from pka.ingestion.arxiv import parse_arxiv_url
    from pka.ingestion.biorxiv import parse_biorxiv_url
    from pka.ingestion.direct_mit import DirectMitArticle, parse_direct_mit_url
    from pka.ingestion.doi_org import parse_doi_url
    from pka.ingestion.mitpress import parse_mitpress_url
    from pka.ingestion.nature import parse_nature_url
    from pka.ingestion.sciencedirect import parse_sciencedirect_url
    from pka.ingestion.springer import parse_springer_url
    from pka.ingestion.wikipedia import parse_wikipedia_url

    pdf = _url_looks_like_pdf(url)
    wayback = cfg.fetch_wayback_fallback
    wikipedia = parse_wikipedia_url(url) is not None
    preprint = parse_arxiv_url(url) is not None or parse_biorxiv_url(url) is not None
    direct_mit = parse_direct_mit_url(url)
    doi = any(
        parse(url) is not None
        for parse in (parse_doi_url, parse_nature_url, parse_springer_url, parse_aps_url)
    ) or isinstance(direct_mit, DirectMitArticle)
    pii = parse_sciencedirect_url(url) is not None
    # Both routes that reach openlibrary.py: an MIT Press ISBN, and a
    # direct.mit.edu book title (parse returns the title as a bare str).
    book_lookup = parse_mitpress_url(url) is not None or isinstance(direct_mit, str)
    try:
        return await asyncio.wait_for(
            _fetch_one_impl(client, doc_id, url, slot_held=slot_held),
            timeout=_fetch_budget_seconds(
                pdf=pdf,
                wayback=wayback,
                wikipedia=wikipedia,
                preprint=preprint,
                doi=doi,
                pii=pii,
                book_lookup=book_lookup,
            ),
        )
    except TimeoutError:
        return FetchResult(doc_id, url, "unfetchable", None, None, "timeout")


# ── DB helpers ────────────────────────────────────────────────────────────────


def _get_pending(
    limit: int | None = None,
    source: Source | str = Source.FIREFOX,
) -> list[tuple[int, str]]:
    """Return (document_id, url) rows with fetch_status='pending' for ``source``."""
    eng = get_engine()
    q = sa.select(documents.c.id, documents.c.url_or_path).where(
        (documents.c.source == str(source)) & (documents.c.fetch_status == str(FetchStatus.PENDING))
    )
    if limit:
        q = q.limit(limit)
    with eng.connect() as con:
        return [(r[0], r[1]) for r in con.execute(q).fetchall() if r[1]]


def reset_unfetchable_for_fetch(source: Source | str = Source.FIREFOX) -> int:
    """Re-queue unfetchable URLs for ``source`` whose last attempt is old enough
    that the failure may no longer hold (server back up, network fixed, rate
    limit expired). Skips URLs with a structural reason to fail (local paths,
    unsupported schemes), since retrying those can never succeed.
    """
    eng = get_engine()
    cutoff = int(time.time()) - cfg.fetch_unfetchable_retry_after_seconds
    last_attempt = (
        sa.select(
            fetch_log.c.document_id,
            sa.func.max(fetch_log.c.timestamp).label("last_ts"),
        )
        .group_by(fetch_log.c.document_id)
        .subquery()
    )
    with eng.begin() as con:
        rows = con.execute(
            sa.select(documents.c.id, documents.c.url_or_path)
            .select_from(
                documents.outerjoin(last_attempt, last_attempt.c.document_id == documents.c.id)
            )
            .where(
                (documents.c.source == str(source))
                & (documents.c.fetch_status == str(FetchStatus.UNFETCHABLE))
                & (sa.or_(last_attempt.c.last_ts.is_(None), last_attempt.c.last_ts <= cutoff))
            )
        ).fetchall()
        requeue_ids = [
            row[0] for row in rows if row[1] and bookmark_url_unfetchable_reason(row[1]) is None
        ]
        if not requeue_ids:
            return 0
        result = con.execute(
            documents.update()
            .where(documents.c.id.in_(requeue_ids))
            .values(fetch_status=str(FetchStatus.PENDING))
        )
        count = result.rowcount or 0
    if count > 0:
        log.info("Re-queued %d previously unfetchable URLs (retry cooldown elapsed)", count)
    return count


def _persist_fetch_result(r: FetchResult) -> None:
    """Write one fetch outcome immediately so progress survives cancel/crash."""
    eng = get_engine()
    now = int(time.time())
    with eng.begin() as con:
        update_values: dict = {"fetch_status": r.status}
        if r.status == "fetched":
            update_values["archive_url"] = r.archive_url
        if r.title:
            update_values["title"] = r.title
        if r.card_summary:
            update_values["card_summary"] = r.card_summary
        if r.doi:
            update_values["doi"] = r.doi
        if r.arxiv_id:
            update_values["arxiv_id"] = r.arxiv_id
        if r.year:
            update_values["year"] = r.year
        if r.authors_json:
            update_values["authors_json"] = r.authors_json
        if r.isbn:
            update_values["isbn"] = r.isbn
        con.execute(
            documents.update().where(documents.c.id == r.document_id).values(**update_values)
        )
        con.execute(
            fetch_log.insert().values(
                document_id=r.document_id,
                timestamp=now,
                http_status=r.http_status,
                error_msg=r.error_msg,
            )
        )


def _empty_embed_stats() -> dict:
    return {"processed": 0, "skipped": 0, "failed": 0, "chunks": 0}


def _accumulate_embed(embed_stats: dict, outcome: dict) -> bool:
    """Merge one embed outcome into stats. Returns True when embed failed."""
    if outcome.get("failed"):
        embed_stats["failed"] += 1
        return True
    if outcome.get("skipped"):
        embed_stats["skipped"] += 1
        return False
    if outcome.get("processed"):
        embed_stats["processed"] += 1
        embed_stats["chunks"] += outcome.get("chunks", 0)
    return False


# ── Shared worker pool ────────────────────────────────────────────────────────


async def _run_fetch_workers(
    work: list[tuple[int, str]],
    *,
    concurrency: int | None,
    progress_key: str | None,
    advance_phase: str | None = None,
    on_result: Callable[[int, FetchResult], None] | None = None,
    embed_fn: Callable[..., dict] | None = None,
    embed_stats: dict | None = None,
    dry_run: bool = False,
) -> tuple[list[FetchResult], str | None]:
    """Run the per-domain fetch worker pool over ``work`` items.

    Returns ``(results, stopped_reason)``. ``on_result`` runs after each fetch is
    persisted (used to collect texts). When ``embed_stats`` is provided the embed
    branch is enabled: each fetched doc is embedded inline via ``embed_fn`` and the
    outcome merged into ``embed_stats`` (matching ``fetch_and_embed_pending``).
    """
    workers = concurrency if concurrency is not None else cfg.fetch_concurrency
    results: list[FetchResult] = []
    stopped: str | None = None
    queue = _DomainQueue(work)

    async def worker(client: httpx.AsyncClient) -> None:
        nonlocal stopped
        while True:
            if progress_key and should_stop(progress_key):
                stopped = should_stop(progress_key)
                break
            drawn = queue.get()
            if drawn is None:
                break
            (doc_id, url), delay, slot_held = drawn
            if delay > 0:
                await asyncio.sleep(delay)
            failed = False
            r: FetchResult | None = None
            try:
                r = await _fetch_one(client, doc_id, url, slot_held=slot_held)
                results.append(r)
                failed = r.status == "unfetchable"
                if on_result:
                    on_result(doc_id, r)
                await asyncio.to_thread(_persist_fetch_result, r)
            finally:
                if progress_key:
                    advance(progress_key, phase=advance_phase, failed=failed)

            if embed_stats is not None:
                if progress_key and should_stop(progress_key):
                    stopped = should_stop(progress_key)
                    break
                if embed_fn and r and r.text and not dry_run:
                    try:
                        outcome = await asyncio.to_thread(
                            embed_fn,
                            doc_id,
                            r.text,
                            r.card_summary,
                        )
                        _accumulate_embed(embed_stats, outcome)
                    except Exception as exc:
                        log.exception("Embed failed for doc_id=%d: %s", doc_id, exc)
                        embed_stats["failed"] += 1

    async with httpx.AsyncClient(
        headers={"User-Agent": cfg.fetch_user_agent},
        timeout=_http_timeout(),
    ) as client:
        await asyncio.gather(*(worker(client) for _ in range(workers)))

    return results, stopped


# ── Public entry points ───────────────────────────────────────────────────────


async def fetch_and_embed_pending(
    limit: int | None = 500,
    concurrency: int | None = None,
    progress_key: str | None = None,
    embed_fn: Callable[..., dict] | None = None,
    dry_run: bool = False,
    source: Source | str = Source.FIREFOX,
) -> dict:
    """
    Fetch a source's document URLs and embed each document before moving to the next.

    Work queue includes pending URLs and fetched docs missing chunks (orphan backfill).
    """
    from pka.db.queries import source_ingest_queue

    reset_unfetchable_for_fetch(source)

    workers = concurrency if concurrency is not None else cfg.fetch_concurrency
    work = source_ingest_queue(source, limit)
    if not work:
        log.info("No %s URLs to fetch and embed", source)
        return {
            "fetched": 0,
            "skipped": 0,
            "unfetchable": 0,
            "no_text_layer": 0,
            "embed": _empty_embed_stats(),
        }

    log.info(
        "Fetching and embedding %d URLs (concurrency=%d, timeout=%ss)",
        len(work),
        workers,
        cfg.fetch_timeout_seconds,
    )
    embed_stats = _empty_embed_stats()
    results, stopped = await _run_fetch_workers(
        work,
        concurrency=concurrency,
        progress_key=progress_key,
        advance_phase="fetching",
        embed_fn=embed_fn,
        embed_stats=embed_stats,
        dry_run=dry_run,
    )

    stats = {
        "fetched": sum(1 for r in results if r.status == "fetched"),
        "skipped": sum(1 for r in results if r.status == "skipped"),
        "unfetchable": sum(1 for r in results if r.status == "unfetchable"),
        "no_text_layer": sum(1 for r in results if r.status == str(FetchStatus.NO_TEXT_LAYER)),
        "embed": embed_stats,
    }
    if stopped:
        stats["stopped"] = stopped
    elif progress_key and should_stop(progress_key):
        stats["stopped"] = should_stop(progress_key)
    log.info(
        "Fetch and embed complete: fetch=%s embed=%s",
        {k: v for k, v in stats.items() if k not in ("embed", "stopped")},
        embed_stats,
    )
    return stats


async def fetch_pending(
    limit: int | None = 500,
    concurrency: int | None = None,
    progress_key: str | None = None,
) -> dict:
    """
    Fetch pending Firefox bookmark URLs.
    Returns stats dict and a {document_id: text} mapping for further ingestion.
    Pass ``limit=None`` to fetch all pending URLs.
    """
    reset_unfetchable_for_fetch()

    workers = concurrency if concurrency is not None else cfg.fetch_concurrency
    pending = _get_pending(limit)
    if not pending:
        log.info("No pending URLs to fetch")
        return {"fetched": 0, "skipped": 0, "unfetchable": 0, "no_text_layer": 0, "texts": {}}

    log.info(
        "Fetching %d pending URLs (concurrency=%d, timeout=%ss)",
        len(pending),
        workers,
        cfg.fetch_timeout_seconds,
    )
    fetched_texts: dict[int, str] = {}

    def _collect_text(doc_id: int, r: FetchResult) -> None:
        if r.text:
            fetched_texts[doc_id] = r.text

    results, stopped = await _run_fetch_workers(
        pending,
        concurrency=concurrency,
        progress_key=progress_key,
        on_result=_collect_text,
    )

    stats = {
        "fetched": sum(1 for r in results if r.status == "fetched"),
        "skipped": sum(1 for r in results if r.status == "skipped"),
        "unfetchable": sum(1 for r in results if r.status == "unfetchable"),
        "no_text_layer": sum(1 for r in results if r.status == str(FetchStatus.NO_TEXT_LAYER)),
        "texts": fetched_texts,
    }
    if stopped:
        stats["stopped"] = stopped
    log.info("Fetch complete: %s", {k: v for k, v in stats.items() if k != "texts"})
    return stats
