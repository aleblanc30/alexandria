"""Calibre book ingestion."""
from __future__ import annotations

import logging

import sqlalchemy as sa

from pka.connectors.calibre import CalibreBook
from pka.constants import FetchStatus, Source
from pka.db.queries import (
    document_index,
    existing_chunk_count,
    get_engine,
    insert_document_if_new,
    insert_source_collections,
    insert_source_tags,
    source_ids_with_chunks,
    upsert_document,
)
from pka.db.schema import documents
from pka.ingestion.book_extractor import extract_book_text, metadata_text
from pka.ingestion.core import ingest_text_block
from pka.ingestion.loops import MetadataOutcome, run_embed_loop, run_metadata_loop
from pka.ingestion.runners._common import progress_tick, stop_requested

log = logging.getLogger(__name__)


def ingest_calibre_metadata(
    books: list[CalibreBook],
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Persist new Calibre book records without embedding."""
    known = document_index(Source.CALIBRE)

    def _persist(book: CalibreBook) -> MetadataOutcome:
        if dry_run:
            return "dry_run"
        doc_id = insert_document_if_new(
            source       = Source.CALIBRE,
            source_id    = book.source_id,
            title        = book.title,
            url_or_path  = str(book.preferred_path) if book.preferred_path else None,
            date_added   = book.date_added,
            fetch_status = (
                FetchStatus.AVAILABLE if book.preferred_path else FetchStatus.MISSING
            ),
        )
        if doc_id is None:
            return "skipped"
        insert_source_tags(doc_id, book.tags, source=Source.CALIBRE)
        if book.series:
            insert_source_collections(doc_id, [book.series], source=Source.CALIBRE)
        known[book.source_id] = doc_id
        return "processed"

    return run_metadata_loop(
        books,
        known=known,
        get_source_id=lambda b: b.source_id,
        persist=_persist,
        progress_key=progress_key,
    )


def ingest_calibre_books(
    books: list[CalibreBook],
    skip_existing: bool = True,
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Phase 1: embed title + description for every book."""
    doc_ids = document_index(Source.CALIBRE) if skip_existing else {}
    embedded = source_ids_with_chunks(Source.CALIBRE) if skip_existing else set()

    def _should_skip(book: CalibreBook) -> bool:
        return skip_existing and book.source_id in embedded

    def _process(book: CalibreBook) -> tuple[bool, int]:
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
        result = ingest_text_block(
            doc_id,
            metadata_text(book.title, book.description, book.authors),
            Source.CALIBRE,
            extra_metadata={"title": book.title, "pass": "metadata"},
            dry_run=dry_run,
        )
        if result["skipped"]:
            return False, 0
        embedded.add(book.source_id)
        return True, result["chunks_added"]

    return run_embed_loop(
        books,
        should_skip=_should_skip,
        process=_process,
        progress_key=progress_key,
        on_error_log=lambda book, exc: log.exception(
            "Failed calibre book %s: %s", book.source_id, exc,
        ),
    )


def _calibre_doc_id(book: CalibreBook) -> int | None:
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
    """Phase 2: extract and embed full book text."""
    stats = {"processed": 0, "skipped": 0, "failed": 0, "chunks": 0}

    for book in books:
        if (stop := stop_requested(progress_key)):
            stats["stopped"] = stop
            break
        failed = False
        if not book.preferred_path or not book.preferred_path.exists():
            log.debug("No file for book %s — skipping full-text", book.title)
            stats["skipped"] += 1
            progress_tick(progress_key)
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
                result = ingest_text_block(
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
            progress_tick(progress_key, failed=failed)

    return stats
