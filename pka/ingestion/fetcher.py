"""
Async URL fetcher for Firefox bookmark content.
Runs as a background job — does not block the ingestion pipeline.

Strategy:
  - httpx AsyncClient with rate limiting (token bucket, per-domain)
  - trafilatura for main-content extraction; readability-lxml as fallback
  - Remote PDFs: download bytes, extract via book_extractor.extract_pdf, embed
  - Other non-HTML targets (EPUB, torrents, …) flagged as "skipped"
  - Auth failures, timeouts, 4xx/5xx → status "unfetchable", logged to fetch_log
  - HTTP 404 with ``fetch_wayback_fallback`` enabled → query archive.org for a snapshot
  - ``*.wikipedia.org`` URLs → MediaWiki Action API (with retries) instead of HTML scrape

Previously skipped PDF bookmarks stay ``fetch_status=skipped`` until reset manually, e.g.::

    UPDATE documents SET fetch_status = 'pending'
    WHERE source = 'firefox' AND fetch_status = 'skipped' AND url_or_path LIKE '%.pdf';
"""
import asyncio
import logging
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
import sqlalchemy as sa

from pka.config import settings as cfg
from pka.constants import FetchStatus, Source
from pka.db.queries import get_engine
from pka.db.schema import documents, fetch_log
from pka.ingestion.book_extractor import extract_pdf

log = logging.getLogger(__name__)

# MIME types we will attempt to parse as HTML
_HTML_TYPES = {"text/html", "application/xhtml+xml"}
_PDF_TYPES = {"application/pdf"}
_PDF_MAGIC = b"%PDF"

# Extensions that are not HTML and should be skipped (PDF handled separately)
_SKIP_EXTENSIONS = {".epub", ".torrent", ".zip", ".gz", ".mp4", ".mp3"}


@dataclass
class FetchResult:
    document_id: int
    url: str
    status: str         # fetched | unfetchable | skipped
    text: str | None    # extracted main text (if fetched)
    http_status: int | None
    error_msg: str | None
    archive_url: str | None = None  # Wayback snapshot URL when content came from archive.org


# ── Rate limiting (simple per-domain token bucket) ───────────────────────────

class _DomainRateLimiter:
    """Per-domain rate limit; uses threading.Lock so it survives asyncio.run() restarts."""

    def __init__(self, rps: float = 1.0):
        self._rps = rps
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    async def wait(self, url: str) -> None:
        domain = urlparse(url).netloc
        with self._lock:
            now = time.monotonic()
            since = now - self._last.get(domain, 0)
            gap = 1.0 / self._rps
            sleep_for = max(0.0, gap - since)
        if sleep_for:
            await asyncio.sleep(sleep_for)
        with self._lock:
            self._last[domain] = time.monotonic()


_limiter = _DomainRateLimiter(rps=1.0)   # 1 req/s per domain


def _url_looks_like_pdf(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


def _is_pdf_content_type(content_type: str) -> bool:
    base = content_type.split(";")[0].strip().lower()
    return base in _PDF_TYPES


def _is_pdf_bytes(data: bytes) -> bool:
    return len(data) >= 4 and data[:4] == _PDF_MAGIC


def _http_timeout(*, pdf: bool = False) -> httpx.Timeout:
    read = cfg.fetch_pdf_timeout_seconds if pdf else cfg.fetch_timeout_seconds
    connect = cfg.fetch_connect_timeout_seconds
    return httpx.Timeout(connect=connect, read=read, write=connect, pool=connect)


def _fetch_budget_seconds(*, pdf: bool = False, wayback: bool = False) -> float:
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
    return base


def _extract_text_from_pdf_bytes(
    data: bytes,
    *,
    max_pages: int | None = None,
) -> str | None:
    """Write bytes to a temp file and run the Calibre PDF extractor."""
    pages = max_pages if max_pages is not None else cfg.fetch_pdf_max_pages
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        sections = extract_pdf(Path(tmp.name), max_pages=pages)
    if not sections:
        return None
    parts = [s["text"] for s in sections if s.get("text", "").strip()]
    return "\n\n".join(parts) if parts else None


# ── Content extraction ────────────────────────────────────────────────────────

def _extract_text(html: str, url: str) -> str | None:
    # Primary: trafilatura (respects main-content heuristics)
    try:
        import trafilatura
        text = trafilatura.extract(html, url=url, include_comments=False,
                                   include_tables=False)
        if text and len(text.strip()) > 0:
            return text.strip()
    except Exception:
        pass

    # Fallback: readability-lxml
    try:
        from readability import Document
        doc = Document(html)
        import re
        raw = doc.summary()
        text = re.sub(r"<[^>]+>", " ", raw)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            return text
    except Exception:
        pass

    # Last resort: strip tags from the raw HTML
    try:
        import re
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            return text
    except Exception:
        pass

    return None


def _fetch_pdf_result(
    doc_id: int,
    url: str,
    body: bytes,
    http_status: int,
) -> FetchResult:
    if len(body) > cfg.fetch_pdf_max_bytes:
        return FetchResult(
            doc_id, url, "unfetchable", None, http_status,
            f"pdf exceeds {cfg.fetch_pdf_max_bytes} bytes",
        )
    if not _is_pdf_bytes(body):
        return FetchResult(
            doc_id, url, "unfetchable", None, http_status,
            "response is not a PDF",
        )
    text = _extract_text_from_pdf_bytes(body)
    if not text:
        return FetchResult(
            doc_id, url, "unfetchable", None, http_status,
            "pdf extraction yielded no text",
        )
    return FetchResult(doc_id, url, "fetched", text, http_status, None)


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

    if is_wikipedia_special(url):
        return FetchResult(doc_id, url, "skipped", None, None, "wikipedia special page")
    if parse_wikipedia_url(url) is not None:
        return await fetch_wikipedia_with_retries(client, doc_id, url)

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
        return _fetch_pdf_result(doc_id, url, body, http_status)

    if content_type and content_type not in _HTML_TYPES:
        return FetchResult(doc_id, url, "skipped", None, http_status,
                           f"non-html content-type: {content_type}")

    text = _extract_text(resp.text, url)
    if not text:
        return FetchResult(doc_id, url, "unfetchable", None, http_status,
                           "content extraction yielded no text")

    return FetchResult(doc_id, url, "fetched", text, http_status, None)


async def _fetch_one(
    client: httpx.AsyncClient,
    doc_id: int,
    url: str,
) -> FetchResult:
    pdf = _url_looks_like_pdf(url)
    wayback = cfg.fetch_wayback_fallback
    try:
        return await asyncio.wait_for(
            _fetch_one_impl(client, doc_id, url),
            timeout=_fetch_budget_seconds(pdf=pdf, wayback=wayback),
        )
    except asyncio.TimeoutError:
        return FetchResult(doc_id, url, "unfetchable", None, None, "timeout")


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_pending(limit: int | None = None) -> list[tuple[int, str]]:
    """Return (document_id, url) rows with fetch_status='pending' and source='firefox'."""
    eng = get_engine()
    q = sa.select(documents.c.id, documents.c.url_or_path).where(
        (documents.c.source == str(Source.FIREFOX)) &
        (documents.c.fetch_status == str(FetchStatus.PENDING))
    )
    if limit:
        q = q.limit(limit)
    with eng.connect() as con:
        return [(r[0], r[1]) for r in con.execute(q).fetchall() if r[1]]


def pending_firefox_count() -> int:
    return len(_get_pending())


def _persist_fetch_result(r: FetchResult) -> None:
    """Write one fetch outcome immediately so progress survives cancel/crash."""
    eng = get_engine()
    now = int(time.time())
    with eng.begin() as con:
        update_values: dict = {"fetch_status": r.status}
        if r.status == "fetched":
            update_values["archive_url"] = r.archive_url
        con.execute(
            documents.update()
            .where(documents.c.id == r.document_id)
            .values(**update_values)
        )
        con.execute(fetch_log.insert().values(
            document_id = r.document_id,
            timestamp   = now,
            http_status = r.http_status,
            error_msg   = r.error_msg,
        ))


def _write_results(results: list[FetchResult], fetched_texts: dict[int, str]) -> None:
    """Persist a batch of fetch results (mostly for tests)."""
    for r in results:
        _persist_fetch_result(r)


# ── Public entry point ────────────────────────────────────────────────────────

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
    from pka.ingestion.sync_helpers import should_stop

    workers = concurrency if concurrency is not None else cfg.fetch_concurrency
    pending = _get_pending(limit)
    if not pending:
        log.info("No pending URLs to fetch")
        return {"fetched": 0, "skipped": 0, "unfetchable": 0, "texts": {}}

    log.info(
        "Fetching %d pending URLs (concurrency=%d, timeout=%ss)",
        len(pending), workers, cfg.fetch_timeout_seconds,
    )
    sem = asyncio.Semaphore(workers)
    results: list[FetchResult] = []
    fetched_texts: dict[int, str] = {}
    stopped: str | None = None
    queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
    for item in pending:
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
            try:
                async with sem:
                    r = await _fetch_one(client, doc_id, url)
                    results.append(r)
                    if r.text:
                        fetched_texts[doc_id] = r.text
                    failed = r.status == "unfetchable"
                    _persist_fetch_result(r)
            finally:
                if progress_key:
                    from pka.ingestion.sync_progress import advance
                    advance(progress_key, failed=failed)

    async with httpx.AsyncClient(
        headers={"User-Agent": "PKA/0.2 (personal knowledge archive; local-only)"},
        timeout=_http_timeout(),
    ) as client:
        await asyncio.gather(*(worker(client) for _ in range(workers)))

    stats = {
        "fetched":     sum(1 for r in results if r.status == "fetched"),
        "skipped":     sum(1 for r in results if r.status == "skipped"),
        "unfetchable": sum(1 for r in results if r.status == "unfetchable"),
        "texts":       fetched_texts,
    }
    if stopped:
        stats["stopped"] = stopped
    log.info("Fetch complete: %s", {k: v for k, v in stats.items() if k != "texts"})
    return stats
