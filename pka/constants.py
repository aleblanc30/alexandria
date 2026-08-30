"""
Project-wide constants and string-based enums.

These enums are string-valued so existing DB rows (stored as text) remain
compatible. Callers can use either the enum member or the underlying string.
"""
from enum import StrEnum


class Source(StrEnum):
    FIREFOX = "firefox"
    ZOTERO = "zotero"
    CALIBRE = "calibre"
    IMAGE = "image"
    YOUTUBE = "youtube"
    REDDIT = "reddit"


class FetchStatus(StrEnum):
    PENDING = "pending"
    FETCHED = "fetched"
    UNFETCHABLE = "unfetchable"
    SKIPPED = "skipped"
    AVAILABLE = "available"   # Zotero/Calibre asset already on disk
    MISSING = "missing"       # Calibre book with no file
    # The file is present and readable, but no page carries an embedded text
    # layer — a scan. Distinct from UNFETCHABLE (broken/oversized/not a PDF)
    # and from AVAILABLE (never attempted), so the OCR-candidate set is one
    # query rather than a guess. Nothing re-queues it: re-reading the same
    # bytes cannot produce text.
    NO_TEXT_LAYER = "no_text_layer"


class PdfTextLayer(StrEnum):
    """Why a PDF extraction produced the text it did (or produced none)."""

    TEXT = "text"                   # at least one page yielded embedded text
    NONE = "no_text_layer"          # pages exist, none carry text — OCR candidate
    UNKNOWN = "unknown"             # a page cap stopped before any text was found
    EMPTY = "empty"                 # opened, but the document has no pages
    UNREADABLE = "unreadable"       # neither pdfplumber nor pypdf could open it


class TagOrigin(StrEnum):
    SOURCE = "source"
    INFERRED = "inferred"
    MANUAL = "manual"
    LLM = "llm"
    CLUSTER_L1 = "cluster_l1"
    CLUSTER_L2 = "cluster_l2"
    LEARNED = "learned"


ALL_SOURCES = [s.value for s in Source]
