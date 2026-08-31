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
  - Auth failures, timeouts, 4xx/5xx → status "unfetchable", logged to fetch_log
  - HTTP 404 with ``fetch_wayback_fallback`` enabled → query archive.org for a snapshot
  - ``*.wikipedia.org`` URLs → MediaWiki Action API (with retries) instead of HTML scrape
  - Amazon book product pages → title + editorial summary extracted for browse cards
  - YouTube video URLs → oEmbed API (title + channel, no API key); title on cards
  - Reddit thread URLs → public ``.json`` listing (title + selftext, or top comments
    for a link post); URL-derived title/subreddit fallback when blocked
  - ``arxiv.org`` URLs → export.arxiv.org API (metadata + PDF); title and abstract on cards
  - ``biorxiv.org`` URLs → api.biorxiv.org DOI lookup (metadata + PDF); title and abstract on cards
  - ``pubmed.ncbi.nlm.nih.gov`` URLs → NCBI efetch (metadata + abstract, no PDF); title and abstract on cards

Previously skipped PDF bookmarks stay ``fetch_status=skipped`` until reset manually, e.g.::

    UPDATE documents SET fetch_status = 'pending'
    WHERE source = 'firefox' AND fetch_status = 'skipped' AND url_or_path LIKE '%.pdf';
"""

import asyncio
import logging
import re
import time
from collections.abc import Callable
from urllib.parse import urlparse

import httpx
import sqlalchemy as sa

from pka.config import settings as cfg
from pka.constants import FetchStatus, Source
from pka.db.queries import get_engine
from pka.db.schema import documents, fetch_log
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
) -> float:
    """Hard ceiling per URL including rate-limit wait and text extraction."""
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
    return base


# ── Per-URL fetch ─────────────────────────────────────────────────────────────


async def _fetch_one_impl(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
) -> FetchResult:
    from pka.ingestion.wikipedia import (
        fetch_wikipedia_with_retries,
        is_wikipedia_special,
        parse_wikipedia_url,
    )

    if reason := bookmark_url_unfetchable_reason(url):
        return FetchResult(doc_id, url, "unfetchable", None, None, reason)

    if is_wikipedia_special(url):
        return FetchResult(doc_id, url, "skipped", None, None, "wikipedia special page")
    if parse_wikipedia_url(url) is not None:
        return await fetch_wikipedia_with_retries(client, doc_id, url)

    from pka.ingestion.youtube_bookmark import fetch_youtube_video, parse_youtube_url

    if parse_youtube_url(url):
        result = await fetch_youtube_video(client, doc_id, url)
        if result is not None:
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

    expect_pdf = _url_looks_like_pdf(url)
    path = urlparse(url).path.lower()
    if not expect_pdf and any(path.endswith(ext) for ext in _SKIP_EXTENSIONS):
        return FetchResult(doc_id, url, "skipped", None, None, "non-html extension")

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

    return FetchResult(doc_id, url, "fetched", text, http_status, None)


async def _fetch_one(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
) -> FetchResult:
    from pka.ingestion.arxiv import parse_arxiv_url
    from pka.ingestion.biorxiv import parse_biorxiv_url
    from pka.ingestion.wikipedia import parse_wikipedia_url

    pdf = _url_looks_like_pdf(url)
    wayback = cfg.fetch_wayback_fallback
    wikipedia = parse_wikipedia_url(url) is not None
    preprint = parse_arxiv_url(url) is not None or parse_biorxiv_url(url) is not None
    try:
        return await asyncio.wait_for(
            _fetch_one_impl(client, doc_id, url),
            timeout=_fetch_budget_seconds(
                pdf=pdf,
                wayback=wayback,
                wikipedia=wikipedia,
                preprint=preprint,
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
    """Re-queue unfetchable URLs for ``source`` before a fetch run (dev mode only)."""
    if not cfg.dev:
        return 0

    eng = get_engine()
    with eng.begin() as con:
        rows = con.execute(
            sa.select(documents.c.id, documents.c.url_or_path).where(
                (documents.c.source == str(source))
                & (documents.c.fetch_status == str(FetchStatus.UNFETCHABLE))
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
        log.info("Re-queued %d previously unfetchable URLs (dev mode)", count)
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
    queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
    for item in work:
        queue.put_nowait(item)
    for _ in range(workers):
        queue.put_nowait(None)

    async def worker(client: httpx.AsyncClient) -> None:
        nonlocal stopped
        while True:
            if progress_key and should_stop(progress_key):
                stopped = should_stop(progress_key)
                break
            item = await queue.get()
            if item is None:
                break
            doc_id, url = item
            failed = False
            r: FetchResult | None = None
            try:
                r = await _fetch_one(client, doc_id, url)
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
