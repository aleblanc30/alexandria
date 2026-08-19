"""
Shared fetch primitives: result type, per-domain rate limit, timeouts, extraction.

Lives below ``fetcher`` so the per-site fetchers (arxiv, biorxiv, wayback,
wikipedia) can use these without importing the dispatcher that calls them —
``fetcher`` dispatches down to those modules, they depend only on this one.
"""
import asyncio
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from pka.config import settings as cfg
from pka.ingestion.book_extractor import extract_pdf

# MIME types we will attempt to parse as HTML
_HTML_TYPES = {"text/html", "application/xhtml+xml"}
_PDF_TYPES = {"application/pdf"}
_PDF_MAGIC = b"%PDF"


@dataclass
class FetchResult:
    document_id: int
    url: str
    status: str         # fetched | unfetchable | skipped
    text: str | None    # extracted main text (if fetched)
    http_status: int | None
    error_msg: str | None
    archive_url: str | None = None  # Wayback snapshot URL when content came from archive.org
    title: str | None = None       # when set, overrides documents.title on persist
    card_summary: str | None = None  # when set, overrides documents.card_summary on persist


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


def _extract_text_from_pdf_bytes(
    data: bytes,
    *,
    max_pages: int | None = None,
) -> str | None:
    """Write bytes to a temp file and run the Calibre PDF extractor."""
    pages = max_pages if max_pages is not None else cfg.fetch_pdf_max_pages
    # delete=False + close before reopening by name: Windows locks the file
    # while a NamedTemporaryFile handle is open.
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp.write(data)
        tmp.close()
        sections = extract_pdf(Path(tmp.name), max_pages=pages)
    finally:
        Path(tmp.name).unlink(missing_ok=True)
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
