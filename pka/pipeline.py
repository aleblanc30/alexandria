"""
Ingestion pipeline orchestrator.

Every connector (Zotero, Firefox, Calibre) ends up routing its text through
:func:`_ingest_text_block`, which is the single chunking → embedding →
persistence path. Image ingestion lives in :mod:`pka.ingestion.image_pipeline`
because it follows a different (multi-pass) flow.
"""
import logging
import uuid

import sqlalchemy as sa

from pka.config import settings as cfg
from pka.connectors.calibre import CalibreBook
from pka.connectors.firefox import FirefoxBookmark
from pka.connectors.zotero import ZoteroItem, zotero_embed_text
from pka.constants import FetchStatus, Source
from pka.db.queries import (
    document_has_chunks,
    document_ids_with_chunks,
    document_index,
    existing_chunk_count,
    get_engine,
    insert_chunks,
    insert_source_collections,
    insert_source_tags,
    source_ids_with_chunks,
    upsert_document,
)
from pka.db.schema import chunks as chunks_table
from pka.db.schema import documents
from pka.ingestion.book_extractor import extract_book_text, metadata_text
from pka.ingestion.chunker import sentence_window_chunks
from pka.storage.vector_store import upsert_chunks

log = logging.getLogger(__name__)


def _progress_tick(key: str | None, *, failed: bool = False) -> None:
    if key:
        from pka.ingestion.sync_progress import advance
        advance(key, failed=failed)


def _stop_requested(key: str | None):
    if not key:
        return None
    from pka.ingestion.sync_helpers import should_stop
    return should_stop(key)


# ─────────────────────────────────────────────────────────────────────────────
# Shared helper — every text-bearing connector flows through this function.
# ─────────────────────────────────────────────────────────────────────────────

def _ingest_text_block(
    doc_id: int,
    text: str,
    source: Source,
    extra_metadata: dict | None = None,
    chunk_offset: int = 0,
    dry_run: bool = False,
    min_chars: int | None = None,
) -> dict:
    """Chunk, embed, and persist a single block of text for a document.

    Args:
        doc_id:         document.id in archive.db.
        text:           cleaned text to chunk.
        source:         :class:`Source` enum value.
        extra_metadata: dict merged into every Chroma metadata record
                        (e.g. ``{"pass": "fulltext"}``).
        chunk_offset:   starting ``chunk_index`` — used by the two-phase
                        Calibre ingestion to avoid colliding with the
                        metadata pass.
        min_chars:      minimum chunk length; defaults to ``cfg.min_chunk_chars``.
                        Use ``1`` for metadata-only passes (title, abstract).
        dry_run:        skip all writes.

    Returns:
        ``{"chunks_added": int, "skipped": bool}``.
    """
    if not text or not text.strip():
        return {"chunks_added": 0, "skipped": True}

    chunk_texts = sentence_window_chunks(
        text,
        window    = cfg.chunk_sentences,
        overlap   = cfg.chunk_overlap,
        min_chars = min_chars if min_chars is not None else cfg.min_chunk_chars,
    )
    if not chunk_texts:
        return {"chunks_added": 0, "skipped": True}

    if dry_run:
        return {"chunks_added": len(chunk_texts), "skipped": False}

    vector_ids = [str(uuid.uuid4()) for _ in chunk_texts]

    base_meta = {
        "document_id": doc_id,
        "source": str(source),
        **(extra_metadata or {}),
    }

    upsert_chunks(
        ids       = vector_ids,
        texts     = chunk_texts,
        metadatas = [
            {**base_meta, "chunk_index": chunk_offset + i}
            for i in range(len(chunk_texts))
        ],
    )
    insert_chunks([
        {
            "document_id": doc_id,
            "chunk_index": chunk_offset + i,
            "text":        t,
            "token_count": len(t.split()),
            "vector_id":   vid,
        }
        for i, (t, vid) in enumerate(zip(chunk_texts, vector_ids))
    ])
    return {"chunks_added": len(chunk_texts), "skipped": False}


# ─────────────────────────────────────────────────────────────────────────────
# Zotero
# ─────────────────────────────────────────────────────────────────────────────

def ingest_zotero_items(
    items: list[ZoteroItem],
    skip_existing: bool = True,
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    stats = {"processed": 0, "skipped": 0, "failed": 0, "chunks": 0}

    for item in items:
        if (stop := _stop_requested(progress_key)):
            stats["stopped"] = stop
            break
        failed = False
        try:
            doc_id = upsert_document(
                source       = Source.ZOTERO,
                source_id    = item.source_id,
                title        = item.title,
                url_or_path  = str(item.pdf_path) if item.pdf_path else item.doi,
                date_added   = item.date_added,
                fetch_status = (
                    FetchStatus.AVAILABLE if item.pdf_path else FetchStatus.PENDING
                ),
            )
            insert_source_tags(doc_id, item.tags, source=Source.ZOTERO)
            insert_source_collections(doc_id, item.collections, source=Source.ZOTERO)

            if skip_existing and document_has_chunks(doc_id):
                stats["skipped"] += 1
                continue

            text = zotero_embed_text(item)
            result = _ingest_text_block(
                doc_id, text, Source.ZOTERO,
                extra_metadata={"title": item.title},
                min_chars=1,
                dry_run=dry_run,
            )
            if result["skipped"]:
                stats["skipped"] += 1
            else:
                stats["processed"] += 1
                stats["chunks"] += result["chunks_added"]

        except Exception as exc:
            log.exception("Zotero item %s failed: %s", item.source_id, exc)
            stats["failed"] += 1
            failed = True
        finally:
            _progress_tick(progress_key, failed=failed)

    return stats


def ingest_zotero_metadata(
    items: list[ZoteroItem],
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Persist Zotero items (documents, tags, collections) without embedding."""
    stats = {"processed": 0, "skipped": 0, "failed": 0}

    for item in items:
        if (stop := _stop_requested(progress_key)):
            stats["stopped"] = stop
            break
        failed = False
        try:
            doc_id = upsert_document(
                source       = Source.ZOTERO,
                source_id    = item.source_id,
                title        = item.title,
                url_or_path  = str(item.pdf_path) if item.pdf_path else item.doi,
                date_added   = item.date_added,
                fetch_status = (
                    FetchStatus.AVAILABLE if item.pdf_path else FetchStatus.PENDING
                ),
            )
            insert_source_tags(doc_id, item.tags, source=Source.ZOTERO)
            insert_source_collections(doc_id, item.collections, source=Source.ZOTERO)
            stats["processed"] += 1
        except Exception as exc:
            log.exception("Zotero metadata %s failed: %s", item.source_id, exc)
            stats["failed"] += 1
            failed = True
        finally:
            _progress_tick(progress_key, failed=failed)

    return stats


def ingest_zotero_embed(
    items: list[ZoteroItem],
    skip_existing: bool = True,
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Embed title + abstract for Zotero items already in the database."""
    stats = {"processed": 0, "skipped": 0, "failed": 0, "chunks": 0}
    doc_ids = document_index(Source.ZOTERO) if skip_existing else {}
    embedded = source_ids_with_chunks(Source.ZOTERO) if skip_existing else set()

    for item in items:
        if (stop := _stop_requested(progress_key)):
            stats["stopped"] = stop
            break
        failed = False
        tick = False
        try:
            if skip_existing and item.source_id in embedded:
                stats["skipped"] += 1
                continue

            doc_id = doc_ids.get(item.source_id)
            if doc_id is None:
                doc_id = upsert_document(
                    source       = Source.ZOTERO,
                    source_id    = item.source_id,
                    title        = item.title,
                    url_or_path  = str(item.pdf_path) if item.pdf_path else item.doi,
                    date_added   = item.date_added,
                    fetch_status = (
                        FetchStatus.AVAILABLE if item.pdf_path else FetchStatus.PENDING
                    ),
                )
                doc_ids[item.source_id] = doc_id

            text = zotero_embed_text(item)
            result = _ingest_text_block(
                doc_id, text, Source.ZOTERO,
                extra_metadata={"title": item.title},
                min_chars=1,
                dry_run=dry_run,
            )
            if result["skipped"]:
                stats["skipped"] += 1
            else:
                stats["processed"] += 1
                stats["chunks"] += result["chunks_added"]
                embedded.add(item.source_id)
                tick = True

        except Exception as exc:
            log.exception("Zotero embed %s failed: %s", item.source_id, exc)
            stats["failed"] += 1
            failed = True
            tick = True
        finally:
            if tick:
                _progress_tick(progress_key, failed=failed)

    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Firefox — two-phase: metadata, then fetched HTML text
# ─────────────────────────────────────────────────────────────────────────────

def ingest_firefox_bookmarks(
    bookmarks: list[FirefoxBookmark],
    skip_existing: bool = True,
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Phase 1: persist bookmark metadata. Fetching happens out-of-band."""
    stats = {"processed": 0, "skipped": 0, "failed": 0}

    for bm in bookmarks:
        if (stop := _stop_requested(progress_key)):
            stats["stopped"] = stop
            break
        failed = False
        try:
            doc_id = upsert_document(
                source       = Source.FIREFOX,
                source_id    = bm.source_id,
                title        = bm.title,
                url_or_path  = bm.url,
                date_added   = bm.date_added,
                fetch_status = FetchStatus.PENDING,
            )
            insert_source_tags(doc_id, bm.tags, source=Source.FIREFOX)
            if bm.folder_path:
                insert_source_collections(doc_id, [bm.folder_path], source=Source.FIREFOX)

            stats["processed"] += 1
        except Exception as exc:
            log.exception("Failed bookmark %s: %s", bm.source_id, exc)
            stats["failed"] += 1
            failed = True
        finally:
            _progress_tick(progress_key, failed=failed)

    return stats


def ingest_fetched_texts(
    fetched_texts: dict[int, str],   # {document_id: extracted text}
    skip_existing: bool = True,
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Phase 2: chunk + embed text returned by :mod:`pka.ingestion.fetcher`."""
    stats = {"processed": 0, "skipped": 0, "failed": 0, "chunks": 0}
    chunked = document_ids_with_chunks(Source.FIREFOX) if skip_existing else set()

    for doc_id, text in fetched_texts.items():
        if (stop := _stop_requested(progress_key)):
            stats["stopped"] = stop
            break
        failed = False
        tick = False
        try:
            if skip_existing and doc_id in chunked:
                stats["skipped"] += 1
                continue

            result = _ingest_text_block(doc_id, text, Source.FIREFOX, dry_run=dry_run)
            if result["skipped"]:
                stats["skipped"] += 1
            else:
                stats["processed"] += 1
                stats["chunks"] += result["chunks_added"]
                chunked.add(doc_id)
                tick = True

        except Exception as exc:
            log.exception("Failed embedding doc_id=%d: %s", doc_id, exc)
            stats["failed"] += 1
            failed = True
            tick = True
        finally:
            if tick:
                _progress_tick(progress_key, failed=failed)

    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Calibre — two-phase: metadata pass (fast), full-text pass (slow, deferred)
# ─────────────────────────────────────────────────────────────────────────────

def ingest_calibre_metadata(
    books: list[CalibreBook],
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Persist Calibre book records without embedding."""
    stats = {"processed": 0, "skipped": 0, "failed": 0}

    for book in books:
        if (stop := _stop_requested(progress_key)):
            stats["stopped"] = stop
            break
        failed = False
        try:
            doc_id = upsert_document(
                source       = Source.CALIBRE,
                source_id    = book.source_id,
                title        = book.title,
                url_or_path  = str(book.preferred_path) if book.preferred_path else None,
                date_added   = book.date_added,
                fetch_status = (
                    FetchStatus.AVAILABLE if book.preferred_path else FetchStatus.MISSING
                ),
            )
            insert_source_tags(doc_id, book.tags, source=Source.CALIBRE)
            if book.series:
                insert_source_collections(doc_id, [book.series], source=Source.CALIBRE)
            stats["processed"] += 1
        except Exception as exc:
            log.exception("Failed calibre metadata %s: %s", book.source_id, exc)
            stats["failed"] += 1
            failed = True
        finally:
            _progress_tick(progress_key, failed=failed)

    return stats


def ingest_calibre_books(
    books: list[CalibreBook],
    skip_existing: bool = True,
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Phase 1: embed title + description for every book."""
    stats = {"processed": 0, "skipped": 0, "failed": 0, "chunks": 0}
    doc_ids = document_index(Source.CALIBRE) if skip_existing else {}
    embedded = source_ids_with_chunks(Source.CALIBRE) if skip_existing else set()

    for book in books:
        if (stop := _stop_requested(progress_key)):
            stats["stopped"] = stop
            break
        failed = False
        tick = False
        try:
            if skip_existing and book.source_id in embedded:
                stats["skipped"] += 1
                continue

            doc_id = doc_ids.get(book.source_id)
            if doc_id is None:
                doc_id = upsert_document(
                    source       = Source.CALIBRE,
                    source_id    = book.source_id,
                    title        = book.title,
                    url_or_path  = str(book.preferred_path) if book.preferred_path else None,
                    date_added   = book.date_added,
                    fetch_status = (
                        FetchStatus.AVAILABLE if book.preferred_path else FetchStatus.MISSING
                    ),
                )
                doc_ids[book.source_id] = doc_id
                insert_source_tags(doc_id, book.tags, source=Source.CALIBRE)
                if book.series:
                    insert_source_collections(doc_id, [book.series], source=Source.CALIBRE)

            text = metadata_text(book.title, book.description, book.authors)
            result = _ingest_text_block(
                doc_id, text, Source.CALIBRE,
                extra_metadata={"title": book.title, "pass": "metadata"},
                dry_run=dry_run,
            )
            if result["skipped"]:
                stats["skipped"] += 1
            else:
                stats["processed"] += 1
                stats["chunks"] += result["chunks_added"]
                embedded.add(book.source_id)
                tick = True

        except Exception as exc:
            log.exception("Failed calibre book %s: %s", book.source_id, exc)
            stats["failed"] += 1
            failed = True
            tick = True
        finally:
            if tick:
                _progress_tick(progress_key, failed=failed)

    return stats


def _calibre_doc_id(book: CalibreBook) -> int | None:
    """Look up an existing Calibre document by source_id."""
    with get_engine().connect() as con:
        row = con.execute(
            sa.select(documents.c.id).where(
                (documents.c.source == str(Source.CALIBRE)) &
                (documents.c.source_id == book.source_id)
            )
        ).fetchone()
    return row[0] if row else None


def ingest_calibre_fulltext(
    books: list[CalibreBook],
    force: bool = False,
    dry_run: bool = False,
    max_pages: int | None = None,
    progress_key: str | None = None,
) -> dict:
    """Phase 2: extract and embed full book text.

    Skips books that lack a resolvable ``preferred_path`` or for which phase 1
    has not been run. Chunk indices are offset past any existing rows so that
    full-text chunks coexist with metadata chunks in a single document.
    """
    stats = {"processed": 0, "skipped": 0, "failed": 0, "chunks": 0}

    for book in books:
        if (stop := _stop_requested(progress_key)):
            stats["stopped"] = stop
            break
        failed = False
        if not book.preferred_path or not book.preferred_path.exists():
            log.debug("No file for book %s — skipping full-text", book.title)
            stats["skipped"] += 1
            _progress_tick(progress_key)
            continue

        try:
            doc_id = _calibre_doc_id(book)
            if doc_id is None:
                log.warning("Book %s not found in DB — run phase 1 first", book.source_id)
                stats["skipped"] += 1
                continue

            sections = extract_book_text(book.preferred_path, max_pages=max_pages)
            if not sections:
                stats["skipped"] += 1
                continue

            chunk_offset = existing_chunk_count(doc_id)
            total_added = 0

            for section in sections:
                result = _ingest_text_block(
                    doc_id,
                    section["text"],
                    Source.CALIBRE,
                    extra_metadata={
                        "title": book.title,
                        "pass":  "fulltext",
                        "section_title": section.get("title", ""),
                        "section_index": section.get("index", 0),
                    },
                    chunk_offset=chunk_offset + total_added,
                    dry_run=dry_run,
                )
                if not result["skipped"]:
                    total_added += result["chunks_added"]

            if total_added == 0:
                stats["skipped"] += 1
            else:
                stats["processed"] += 1
                stats["chunks"] += total_added

        except Exception as exc:
            log.exception("Full-text failed for %s: %s", book.source_id, exc)
            stats["failed"] += 1
            failed = True
        finally:
            _progress_tick(progress_key, failed=failed)

    return stats
