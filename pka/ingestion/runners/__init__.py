"""Per-source ingestion runners."""

from pka.ingestion.runners.calibre import (
    ingest_calibre_books,
    ingest_calibre_fulltext,
    ingest_calibre_metadata,
)
from pka.ingestion.runners.firefox import ingest_fetched_texts, ingest_firefox_bookmarks
from pka.ingestion.runners.zotero import (
    ingest_zotero_embed,
    ingest_zotero_items,
    ingest_zotero_metadata,
)

__all__ = [
    "ingest_calibre_books",
    "ingest_calibre_fulltext",
    "ingest_calibre_metadata",
    "ingest_fetched_texts",
    "ingest_firefox_bookmarks",
    "ingest_zotero_embed",
    "ingest_zotero_items",
    "ingest_zotero_metadata",
]
