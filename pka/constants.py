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
    AVAILABLE = "available"  # Zotero/Calibre asset already on disk
    MISSING = "missing"  # Calibre book with no file
    # The file is present and readable, but no page carries an embedded text
    # layer — a scan. Distinct from UNFETCHABLE (broken/oversized/not a PDF)
    # and from AVAILABLE (never attempted), so the OCR-candidate set is one
    # query rather than a guess. Nothing re-queues it: re-reading the same
    # bytes cannot produce text.
    NO_TEXT_LAYER = "no_text_layer"


class PdfTextLayer(StrEnum):
    """Why a PDF extraction produced the text it did (or produced none)."""

    TEXT = "text"  # at least one page yielded embedded text
    NONE = "no_text_layer"  # pages exist, none carry text — OCR candidate
    UNKNOWN = "unknown"  # a page cap stopped before any text was found
    EMPTY = "empty"  # opened, but the document has no pages
    UNREADABLE = "unreadable"  # neither pdfplumber nor pypdf could open it


class EnrichmentKind(StrEnum):
    """What an ``enrichment_runs`` row produced (PURGE_AND_PROVENANCE_PLAN.md §6).

    One kind per artifact, not per backend: swapping the chat provider does not
    change that a summary is a summary, and the run row records which backend
    made it.
    """

    SUMMARY = "summary"
    IMAGE_DESCRIPTION = "image_description"
    OCR = "ocr"
    BOOK_EXTRACT = "book_extract"


class RunStatus(StrEnum):
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TagOrigin(StrEnum):
    SOURCE = "source"
    INFERRED = "inferred"
    MANUAL = "manual"
    LLM = "llm"
    CLUSTER_L1 = "cluster_l1"
    CLUSTER_L2 = "cluster_l2"
    LEARNED = "learned"


ALL_SOURCES = [s.value for s in Source]

# ── Ports ────────────────────────────────────────────────────────────────────
# Deliberately different per environment, so a source checkout's dev server
# never collides with (or, worse, proxies into) a real running production
# instance:
#   - PROD_PORT (8420): the installed/production app — README's production
#     section, scripts/start-server.bat, scripts/stop-server.bat,
#     scripts/upgrade.ps1. Nothing in `pka` reads this; only the launchers do.
#   - DEV_PORT (8421): ``alexandria dev`` (read from here by pka/cli/dev.py).
#     Also hardcoded, because they cannot import this module, in
#     frontend/vite.config.ts's proxy targets and the uvicorn tasks in
#     .vscode/tasks.json / .claude/launch.json.
#   - .vscode/launch.json's debug configs use yet another port, 8000.
PROD_PORT = 8420
DEV_PORT = 8421
