"""
Deprecated — use :mod:`pka.ingestion.core` and :mod:`pka.ingestion.runners`.

Kept for backward compatibility; new code should import from those modules.
"""

from pka.ingestion.core import ingest_text_block as _ingest_text_block
from pka.ingestion.runners import (
    ingest_calibre_books,
    ingest_calibre_fulltext,
    ingest_calibre_metadata,
    ingest_fetched_texts,
    ingest_firefox_bookmarks,
    ingest_zotero_embed,
    ingest_zotero_items,
    ingest_zotero_metadata,
)

__all__ = [
    "_ingest_text_block",
    "ingest_calibre_books",
    "ingest_calibre_fulltext",
    "ingest_calibre_metadata",
    "ingest_fetched_texts",
    "ingest_firefox_bookmarks",
    "ingest_zotero_embed",
    "ingest_zotero_items",
    "ingest_zotero_metadata",
]
