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
from pathlib import Path

log = logging.getLogger(__name__)


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

def extract_pdf(path: Path, max_pages: int | None = None) -> list[dict]:
    """
    Return a list of page-group dicts: {title: str, text: str, index: int}.
    Pages are grouped in blocks of 10 to avoid over-fragmenting.
    """
    pages: list[str] = []

    # Primary: pdfplumber (handles columns and tables better)
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages):
                if max_pages and i >= max_pages:
                    break
                text = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
                if text.strip():
                    pages.append(text)
    except Exception as exc:
        log.debug("pdfplumber failed for %s (%s), trying pypdf", path.name, exc)
        pages = []

    # Fallback: pypdf
    if not pages:
        try:
            import pypdf
            reader = pypdf.PdfReader(str(path))
            for i, page in enumerate(reader.pages):
                if max_pages and i >= max_pages:
                    break
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(text)
        except Exception as exc:
            log.warning("pypdf also failed for %s: %s", path.name, exc)
            return []

    if not pages:
        return []

    # Group pages into chunks of 10 (≈ section granularity)
    PAGE_GROUP = 10
    groups: list[dict] = []
    for i in range(0, len(pages), PAGE_GROUP):
        group_text = "\n\n".join(pages[i : i + PAGE_GROUP])
        groups.append({
            "title": f"Pages {i + 1}–{min(i + PAGE_GROUP, len(pages))}",
            "text":  group_text,
            "index": i // PAGE_GROUP,
        })

    log.debug("Extracted %d page-groups from %s", len(groups), path.name)
    return groups


# ── Unified entry point ────────────────────────────────────────────────────

def extract_book_text(path: Path, **kwargs) -> list[dict]:
    """
    Dispatch to the right extractor based on file extension.
    Returns [] if format is unsupported or extraction fails.
    """
    ext = path.suffix.lower()
    if ext == ".epub":
        return extract_epub(path, **kwargs)
    elif ext == ".pdf":
        return extract_pdf(path, **kwargs)
    else:
        log.debug("Unsupported format for full-text extraction: %s", ext)
        return []


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
