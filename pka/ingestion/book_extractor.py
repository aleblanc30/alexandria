"""
Text extraction from EPUB and PDF files for Calibre books.

Strategy (matches spec):
  - Metadata pass  : title + description used immediately for embeddings.
  - Full-text pass : triggered per-book or in batch; deferred by default.

EPUB: ebooklib → iterate spine items → extract text per chapter.
PDF:  pdfplumber (layout-aware) with pypdf fallback.

HTML tag stripping is handled here so chunker.py always receives clean text.
"""
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from pka.constants import PdfTextLayer

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BookExtraction:
    """Extracted sections plus why there are (or are not) any of them."""

    sections: list[dict] = field(default_factory=list)
    status: PdfTextLayer = PdfTextLayer.TEXT
    page_count: int = 0     # pages in the file, uncapped (PDF only)
    text_pages: int = 0     # pages that yielded a text layer (PDF only)


# ── HTML stripping ─────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_SPACE = re.compile(r" {2,}")
_MULTI_NL = re.compile(r"\n{3,}")


def strip_html(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    text = _MULTI_SPACE.sub(" ", text)
    text = _MULTI_NL.sub("\n\n", text)
    return text.strip()


# ── EPUB extraction ────────────────────────────────────────────────────────

def extract_epub(path: Path, max_chars_per_chapter: int | None = None) -> list[dict]:
    """
    Return a list of chapter dicts: {title: str, text: str, index: int}.
    Each chapter is a separate item for chunking.
    """
    try:
        import ebooklib
        from ebooklib import epub
    except ImportError:
        log.error("ebooklib not installed — cannot extract EPUB")
        return []

    chapters: list[dict] = []
    try:
        book = epub.read_epub(str(path), options={"ignore_ncx": False})
    except Exception as exc:
        log.warning("Failed to open EPUB %s: %s", path, exc)
        return []

    spine_ids = {item_id for item_id, _ in book.spine}

    for idx, item in enumerate(book.get_items_of_type(ebooklib.ITEM_DOCUMENT)):
        if item.get_id() not in spine_ids:
            continue
        try:
            raw_html = item.get_body_content().decode("utf-8", errors="replace")
        except Exception:
            continue

        text = strip_html(raw_html)
        if not text.strip():
            continue

        if max_chars_per_chapter:
            text = text[:max_chars_per_chapter]

        # Try to get chapter title from <title> or first <h*> tag
        title_match = re.search(r"<(?:title|h[1-3])[^>]*>(.*?)</(?:title|h[1-3])>",
                                 raw_html, re.IGNORECASE | re.DOTALL)
        ch_title = strip_html(title_match.group(1)) if title_match else f"Chapter {idx + 1}"

        chapters.append({"title": ch_title, "text": text, "index": idx})

    log.debug("Extracted %d chapters from %s", len(chapters), path.name)
    return chapters


# ── PDF extraction ─────────────────────────────────────────────────────────

# Pages per section (≈ section granularity); page numbers below are the real
# 1-based numbers from the file, so a page with no text layer does not shift
# the labels of the pages after it.
PAGE_GROUP = 10


def _pages_via_pdfplumber(
    path: Path, max_pages: int | None,
) -> tuple[list[tuple[int, str]], int] | None:
    """``([(page_no, text), …], page_count)``, or None when the file won't open."""
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            page_count = len(pdf.pages)
            found: list[tuple[int, str]] = []
            for i, page in enumerate(pdf.pages):
                if max_pages and i >= max_pages:
                    break
                text = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
                if text.strip():
                    found.append((i + 1, text))
            return found, page_count
    except Exception as exc:
        log.debug("pdfplumber failed for %s (%s), trying pypdf", path.name, exc)
        return None


def _pages_via_pypdf(
    path: Path, max_pages: int | None,
) -> tuple[list[tuple[int, str]], int] | None:
    """Same contract as :func:`_pages_via_pdfplumber`, using pypdf."""
    try:
        import pypdf
        reader = pypdf.PdfReader(str(path))
        page_count = len(reader.pages)
        found: list[tuple[int, str]] = []
        for i, page in enumerate(reader.pages):
            if max_pages and i >= max_pages:
                break
            text = page.extract_text() or ""
            if text.strip():
                found.append((i + 1, text))
        return found, page_count
    except Exception as exc:
        log.warning("pypdf also failed for %s: %s", path.name, exc)
        return None


def _page_groups(pages: list[tuple[int, str]]) -> list[dict]:
    groups: list[dict] = []
    for i in range(0, len(pages), PAGE_GROUP):
        block = pages[i : i + PAGE_GROUP]
        first, last = block[0][0], block[-1][0]
        groups.append({
            "title":      f"Pages {first}–{last}",
            "text":       "\n\n".join(text for _, text in block),
            "index":      i // PAGE_GROUP,
            "page_start": first,
            "page_end":   last,
        })
    return groups


def extract_pdf_report(path: Path, max_pages: int | None = None) -> BookExtraction:
    """Extract a PDF *and* say why the result looks the way it does.

    An empty ``sections`` list has three very different causes — a scan, a
    broken file, and a document with no pages — and callers need to tell them
    apart: only the first is worth OCR, and only the first should be recorded
    as such (``FetchStatus.NO_TEXT_LAYER``). pdfplumber yielding nothing is not
    yet a verdict, so pypdf gets a second opinion before one is reached.
    """
    pages: list[tuple[int, str]] = []
    page_count = 0
    readable = False

    for reader in (_pages_via_pdfplumber, _pages_via_pypdf):
        result = reader(path, max_pages)
        if result is None:
            continue
        readable = True
        found, count = result
        page_count = max(page_count, count)
        if found:
            pages = found
            break

    if not readable:
        return BookExtraction([], PdfTextLayer.UNREADABLE)
    if page_count == 0:
        return BookExtraction([], PdfTextLayer.EMPTY)
    if not pages:
        # A page cap that stopped short of the whole file proves nothing: a
        # scanned cover in front of a text body reads exactly like a scan.
        capped = max_pages is not None and max_pages < page_count
        status = PdfTextLayer.UNKNOWN if capped else PdfTextLayer.NONE
        log.debug(
            "No text layer in first %s page(s) of %s (status=%s)",
            max_pages if capped else page_count, path.name, status,
        )
        return BookExtraction([], status, page_count=page_count)

    groups = _page_groups(pages)
    log.debug("Extracted %d page-groups from %s", len(groups), path.name)
    return BookExtraction(
        groups, PdfTextLayer.TEXT, page_count=page_count, text_pages=len(pages),
    )


def extract_pdf(path: Path, max_pages: int | None = None) -> list[dict]:
    """
    Return a list of page-group dicts:
    {title, text, index, page_start, page_end}. Pages are grouped in blocks of
    ``PAGE_GROUP`` to avoid over-fragmenting.
    """
    return extract_pdf_report(path, max_pages=max_pages).sections


# ── Unified entry point ────────────────────────────────────────────────────

def extract_book_report(
    path: Path,
    max_pages: int | None = None,
    max_chars_per_chapter: int | None = None,
) -> BookExtraction:
    """Dispatch to the right extractor, keeping the diagnosis (see
    :func:`extract_pdf_report`).

    The two limits are per-format and each is ignored by the other extractor —
    a caller with a page budget can pass it without knowing which format it
    has. EPUB never reports ``NO_TEXT_LAYER``: an EPUB with no readable spine
    is a broken file, not a scan, and there is nothing to OCR.
    """
    ext = path.suffix.lower()
    if ext == ".pdf":
        return extract_pdf_report(path, max_pages=max_pages)
    if ext == ".epub":
        chapters = extract_epub(path, max_chars_per_chapter=max_chars_per_chapter)
        return BookExtraction(
            chapters, PdfTextLayer.TEXT if chapters else PdfTextLayer.EMPTY,
        )
    log.debug("Unsupported format for full-text extraction: %s", ext)
    return BookExtraction([], PdfTextLayer.UNREADABLE)


def extract_book_text(
    path: Path,
    max_pages: int | None = None,
    max_chars_per_chapter: int | None = None,
) -> list[dict]:
    """
    Dispatch to the right extractor based on file extension.
    Returns [] if format is unsupported or extraction fails.
    """
    return extract_book_report(
        path, max_pages=max_pages, max_chars_per_chapter=max_chars_per_chapter,
    ).sections


def metadata_text(title: str, description: str | None, authors: list[str]) -> str:
    """
    Produce a short text blob from metadata alone — used for the fast
    initial embedding pass before full text extraction is triggered.
    """
    parts = [title]
    if authors:
        parts.append("by " + ", ".join(authors))
    if description:
        parts.append(strip_html(description))
    return "\n\n".join(filter(None, parts))
