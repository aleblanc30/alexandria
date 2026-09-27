"""
Shared fetch primitives: result type, rate limiter instance, timeouts, extraction.

Lives below ``fetcher`` so the per-site fetchers (arxiv, biorxiv, wayback,
wikipedia) can use these without importing the dispatcher that calls them —
``fetcher`` dispatches down to those modules, they depend only on this one.
"""

import logging
import re
import tempfile
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

import httpx

from pka.config import settings as cfg
from pka.constants import FetchStatus, PdfTextLayer
from pka.ingestion.book_extractor import BookExtraction, extract_pdf_report
from pka.ingestion.rate_limit import AsyncRateLimiter

# MIME types we will attempt to parse as HTML
_HTML_TYPES = {"text/html", "application/xhtml+xml"}
_PDF_TYPES = {"application/pdf"}
_PDF_MAGIC = b"%PDF"


@dataclass
class FetchResult:
    document_id: int
    url: str
    status: str  # fetched | unfetchable | skipped | no_text_layer
    text: str | None  # extracted main text (if fetched)
    http_status: int | None
    error_msg: str | None
    archive_url: str | None = None  # Wayback snapshot URL when content came from archive.org
    title: str | None = None  # when set, overrides documents.title on persist
    card_summary: str | None = None  # when set, overrides documents.card_summary on persist
    # Structured bibliographic fields, set by the identifier-resolving fetch
    # handlers (arXiv, bioRxiv, PubMed, and the DOI/ISBN publisher handlers).
    # Written when present, never blanked.
    doi: str | None = None
    arxiv_id: str | None = None
    year: int | None = None
    authors_json: str | None = None
    # documents.isbn is a join key: only a checksum-valid ISBN reaches it
    # (openlibrary.normalize_isbn / isbn_checksum_valid).
    isbn: str | None = None


log = logging.getLogger(__name__)


# ── Rate limiting ─────────────────────────────────────────────────────────────

_limiter = AsyncRateLimiter(rps=1.0)  # 1 req/s per domain


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


def _extract_pdf_from_bytes(
    data: bytes,
    *,
    max_pages: int | None = None,
) -> BookExtraction:
    """Write bytes to a temp file and run the Calibre PDF extractor."""
    pages = max_pages if max_pages is not None else cfg.fetch_pdf_max_pages
    # delete=False + close before reopening by name: Windows locks the file
    # while a NamedTemporaryFile handle is open.
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp.write(data)
        tmp.close()
        return extract_pdf_report(Path(tmp.name), max_pages=pages)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def _sections_text(sections: list[dict]) -> str | None:
    """Flatten page-group sections into one body string.

    The per-section page range is dropped here: this route embeds a fetched
    document as a single block (``fetched_embed_text``), so there is nowhere to
    hang it. Only the Calibre route keeps it (DESIGN.md §3).
    """
    parts = [s["text"] for s in sections if s.get("text", "").strip()]
    return "\n\n".join(parts) if parts else None


# ── Content extraction ────────────────────────────────────────────────────────


# Optional extractors already reported absent. A package that is not installed
# is an operator-visible deployment fault, but it is the *same* fault on every
# page of a sync, so it is reported once per process rather than per document.
_MISSING_EXTRACTORS: set[str] = set()


def _warn_missing_extractor(package: str) -> None:
    """Report an uninstalled extractor once, so it cannot read as an empty page."""
    if package in _MISSING_EXTRACTORS:
        return
    _MISSING_EXTRACTORS.add(package)
    log.warning(
        "%s is not installed; HTML extraction is falling back to a cruder rung. "
        "Reinstall the project (pip install -e .) to restore it.",
        package,
    )


# Elements whose *contents* are code or presentation, not prose. Dropped before
# the text is taken, so a page whose main content trafilatura cannot find does
# not contribute its inline JavaScript and JSON-LD to the archive — "function
# amw_confirm() { return confirm('Are you sure?') } { "@context" :
# "http://schema.org" ..." as the opening chunk, and hence as the card summary
# via body_excerpt().
_NON_PROSE_XPATH = "//script|//style|//noscript|//template|//svg"
# Only for the degraded path below, when lxml cannot parse the input at all.
_ANY_TAG = re.compile(r"<[^>]+>")


def _tags_to_text(markup: str) -> str:
    """Markup to plain text, with a real parser rather than a tag regex.

    This runs only where trafilatura *and* readability have already failed, so
    its input is the malformed end of the web — which is precisely where a
    regex stripper goes wrong. Two failures that cost the archive real damage:
    an unclosed ``<script>`` (nothing for ``</script>`` to match, so the whole
    tail leaks as prose) and a ``>`` inside an attribute value (``<div title="a
    > b">`` ends the "tag" early, leaking ``b">``). An HTML parser applies the
    spec's tokenizer to both, and decodes entities on the way out, so ``&#160;``
    and ``&amp;`` reach the chunker as characters.

    lxml arrives with trafilatura and readability-lxml but is declared directly
    in ``pyproject.toml``: a transitive dependency that the code imports by name
    is one ``pip install`` away from vanishing.
    """
    if not markup or not markup.strip():
        return ""
    try:
        import lxml.html
    except ImportError:
        _warn_missing_extractor("lxml")
    else:
        try:
            doc = lxml.html.fromstring(markup)
            for element in doc.xpath(_NON_PROSE_XPATH):
                element.drop_tree()
            # itertext() rather than text_content(): the latter concatenates
            # nodes with no separator, gluing a heading to the paragraph after
            # it ("An Anarchist FAQIntroduction"). Joining on a space keeps the
            # element boundary the old tag-substitution gave us for free.
            return " ".join(" ".join(doc.itertext()).split())
        except Exception:  # noqa: BLE001 - malformed markup degrades, it never drops the fetch
            log.debug("lxml could not parse %d chars of markup", len(markup), exc_info=True)
    # A document lxml cannot parse at all (empty, or not markup) still gets the
    # old best-effort treatment rather than dropping the fetch.
    text = _ANY_TAG.sub(" ", markup)
    return " ".join(unescape(text).split())


def _extract_text(html: str, url: str) -> str | None:
    """Best available plain text for a page, trying three rungs in quality order.

    The import and the extraction are caught separately at every rung. Wrapped
    together, an uninstalled package and a page the package could not parse
    produce the same silence, and the archive takes the cruder rung's output for
    every document with nothing in the log to say why.
    """
    # Primary: trafilatura (respects main-content heuristics)
    try:
        import trafilatura
    except ImportError:
        _warn_missing_extractor("trafilatura")
    else:
        try:
            text = trafilatura.extract(html, url=url, include_comments=False, include_tables=False)
            if text and len(text.strip()) > 0:
                return text.strip()
        except Exception:  # noqa: BLE001 - this page falls to the next rung, the sync goes on
            log.debug("trafilatura could not extract %s", url, exc_info=True)

    # Fallback: readability-lxml
    try:
        from readability import Document
    except ImportError:
        _warn_missing_extractor("readability-lxml")
    else:
        try:
            doc = Document(html)
            text = _tags_to_text(doc.summary())
            if text:
                return text
        except Exception:  # noqa: BLE001 - as above: fall through to the last resort
            log.debug("readability could not extract %s", url, exc_info=True)

    # Last resort: strip the raw HTML itself. _tags_to_text carries its own
    # regex fallback, so reaching this handler means even that failed.
    try:
        text = _tags_to_text(html)
        if text:
            return text
    except Exception:  # noqa: BLE001 - an unextractable page is unfetchable, not a crash
        log.debug("last-resort extraction failed for %s", url, exc_info=True)

    return None


def _fetch_pdf_result(
    doc_id: int,
    url: str,
    body: bytes,
    http_status: int,
) -> FetchResult:
    if len(body) > cfg.fetch_pdf_max_bytes:
        return FetchResult(
            doc_id,
            url,
            "unfetchable",
            None,
            http_status,
            f"pdf exceeds {cfg.fetch_pdf_max_bytes} bytes",
        )
    if not _is_pdf_bytes(body):
        return FetchResult(
            doc_id,
            url,
            "unfetchable",
            None,
            http_status,
            "response is not a PDF",
        )
    report = _extract_pdf_from_bytes(body)
    text = _sections_text(report.sections)
    if not text:
        if report.status == PdfTextLayer.NONE:
            # Readable, paginated, and not one page carries text: a scan. Kept
            # apart from "unfetchable" so re-fetching never retries it and the
            # OCR-candidate set stays queryable.
            return FetchResult(
                doc_id,
                url,
                str(FetchStatus.NO_TEXT_LAYER),
                None,
                http_status,
                f"pdf has no text layer ({report.page_count} pages)",
            )
        return FetchResult(
            doc_id,
            url,
            "unfetchable",
            None,
            http_status,
            f"pdf extraction yielded no text ({report.status})",
        )
    return FetchResult(doc_id, url, "fetched", text, http_status, None)
