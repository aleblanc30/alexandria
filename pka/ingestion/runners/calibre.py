"""Calibre book ingestion."""

from __future__ import annotations

import json
import logging

from pka.config import settings as cfg
from pka.connectors.calibre import CalibreBook, split_calibre_tags
from pka.constants import FetchStatus, PdfTextLayer, Source
from pka.db.chunks import existing_chunk_count, source_ids_with_chunks
from pka.db.documents import (
    DocumentWrite,
    document_index,
    insert_document_if_new,
    set_fetch_status,
    upsert_document,
)
from pka.db.tags import insert_source_collections, insert_source_tags
from pka.ingestion.book_extractor import extract_book_report, metadata_text, section_page_range
from pka.ingestion.core import attach_summary_chunk, ingest_text_block
from pka.ingestion.loops import MetadataOutcome, run_embed_loop, run_metadata_loop
from pka.ingestion.openlibrary import isbn_checksum_valid, normalize_isbn
from pka.ingestion.progress import should_stop, tick
from pka.ingestion.text_store import section_blocks, store_document_text, truncate_blocks

log = logging.getLogger(__name__)


def _calibre_isbn(book: CalibreBook) -> str | None:
    """Canonical ISBN, or ``None`` on a failed checksum — reject rather than
    store a typo as a join key."""
    isbn = normalize_isbn(book.isbn) if book.isbn else None
    return isbn if isbn and isbn_checksum_valid(isbn) else None


def _calibre_authors_json(book: CalibreBook) -> str | None:
    return json.dumps(book.authors) if book.authors else None


def _attach_book_synopsis(book: CalibreBook, doc_id: int, *, dry_run: bool) -> int:
    """Attach an external synopsis chunk when the book has none of its own.

    Skipped when Calibre already carries a description: pass 1 embeds that
    already, so a looked-up synopsis would be redundant text and a wasted
    request. The ladder itself (ISBN → Open Library → second catalogue) and the
    ``external_lookup_enabled`` gate live in ``lookup_book``, so with the flag
    off this resolves nothing. Never raises.
    """
    if dry_run or (book.description or "").strip():
        return 0
    from pka.ingestion.openlibrary import lookup_book

    try:
        synopsis = lookup_book(
            title=book.title,
            authors=book.authors,
            isbn=book.isbn,
        )
    except Exception as exc:  # noqa: BLE001 - enrichment is optional; the book still lands
        log.warning("Book lookup failed for %s: %s", book.source_id, exc)
        return 0
    if synopsis is None:
        return 0

    text = synopsis.embed_text()
    if not text:
        return 0

    meta = {
        "title": book.title,
        "pass": "external_synopsis",
        "book_title": synopsis.title or book.title,
        "resolved_by": synopsis.resolved_by,
    }
    if synopsis.isbn:
        meta["isbn"] = synopsis.isbn
    if synopsis.work_key:
        meta["work_key"] = synopsis.work_key

    result = ingest_text_block(
        doc_id,
        text,
        Source.CALIBRE,
        extra_metadata=meta,
        chunk_offset=existing_chunk_count(doc_id),
        min_chars=1,
        refresh=False,  # the caller refreshes once, after this second block
    )
    return 0 if result["skipped"] else result["chunks_added"]


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
        tags, note = split_calibre_tags(book.tags)
        doc_id = insert_document_if_new(
            DocumentWrite(
                source=Source.CALIBRE,
                source_id=book.source_id,
                title=book.title,
                url_or_path=str(book.preferred_path) if book.preferred_path else None,
                date_added=book.date_added,
                fetch_status=(
                    FetchStatus.AVAILABLE if book.preferred_path else FetchStatus.MISSING
                ),
                note=note,
                isbn=_calibre_isbn(book),
                year=book.year,
                authors_json=_calibre_authors_json(book),
            )
        )
        if doc_id is None:
            return "skipped"
        insert_source_tags(doc_id, tags, source=Source.CALIBRE)
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
            tags, note = split_calibre_tags(book.tags)
            doc_id = upsert_document(
                DocumentWrite(
                    source=Source.CALIBRE,
                    source_id=book.source_id,
                    title=book.title,
                    url_or_path=str(book.preferred_path) if book.preferred_path else None,
                    date_added=book.date_added,
                    fetch_status=(
                        FetchStatus.AVAILABLE if book.preferred_path else FetchStatus.MISSING
                    ),
                    note=note,
                    isbn=_calibre_isbn(book),
                    year=book.year,
                    authors_json=_calibre_authors_json(book),
                )
            )
            doc_ids[book.source_id] = doc_id
            insert_source_tags(doc_id, tags, source=Source.CALIBRE)
            if book.series:
                insert_source_collections(doc_id, [book.series], source=Source.CALIBRE)
        result = ingest_text_block(
            doc_id,
            metadata_text(book.title, book.description, book.authors),
            Source.CALIBRE,
            extra_metadata={"title": book.title, "pass": "metadata"},
            dry_run=dry_run,
            fallback_text=book.title,
            refresh=False,
        )
        if result["skipped"]:
            return False, 0
        embedded.add(book.source_id)
        synopsis_chunks = _attach_book_synopsis(book, doc_id, dry_run=dry_run)
        if not dry_run:
            # Both blocks above deferred it; this is the only refresh on the
            # metadata path, so an early return between them would strand the
            # book without an embedding.
            from pka.clustering.doc_embeddings import refresh_document_embedding

            refresh_document_embedding(doc_id)
        return True, result["chunks_added"] + synopsis_chunks

    return run_embed_loop(
        books,
        should_skip=_should_skip,
        process=_process,
        progress_key=progress_key,
        on_error_log=lambda book, exc: log.exception(
            "Failed calibre book %s: %s",
            book.source_id,
            exc,
        ),
    )


def ingest_calibre_fulltext(
    books: list[CalibreBook],
    dry_run: bool = False,
    max_pages: int | None = None,
    progress_key: str | None = None,
) -> dict:
    """Phase 2: extract and embed full book text."""
    stats = {"processed": 0, "skipped": 0, "failed": 0, "chunks": 0, "no_text_layer": 0}
    known = document_index(Source.CALIBRE)

    for book in books:
        if stop := should_stop(progress_key):
            stats["stopped"] = stop
            break
        failed = False
        if not book.preferred_path or not book.preferred_path.exists():
            log.debug("No file for book %s — skipping full-text", book.title)
            stats["skipped"] += 1
            tick(progress_key)
            continue

        try:
            doc_id = known.get(book.source_id)
            if doc_id is None:
                log.warning("Book %s not found in DB — run phase 1 first", book.source_id)
                stats["skipped"] += 1
                continue

            report = extract_book_report(book.preferred_path, max_pages=max_pages)
            if not report.sections:
                if report.status == PdfTextLayer.NONE:
                    # A scan: the file is readable, it just has no text to read.
                    # Recorded rather than silently counted as "skipped", which
                    # is also what an un-run phase 2 looks like — this is the
                    # OCR-candidate set.
                    log.info(
                        "No text layer in %s (%d pages) — marking %s",
                        book.title,
                        report.page_count,
                        FetchStatus.NO_TEXT_LAYER,
                    )
                    if not dry_run:
                        set_fetch_status(doc_id, FetchStatus.NO_TEXT_LAYER)
                    stats["no_text_layer"] += 1
                stats["skipped"] += 1
                continue
            sections = report.sections
            # Retain the extraction before chunking.
            # The file is still on disk, but re-extracting a library costs
            # minutes per book, which is what makes a re-chunk impractical
            # without this. `blocks` maps each section back into `full_text`, so
            # a re-chunk can reproduce the section and page metadata below.
            full_text, full_blocks = section_blocks(sections)
            # Books are capped; fetched pages are not. A few hundred pages of retained prose per book is the only
            # way this sidecar gets expensive, and the opening is what the
            # retention is actually used for. The summary below still sees the
            # whole book — only what is *stored* is cut.
            kept_text, kept_blocks = truncate_blocks(
                full_text,
                full_blocks,
                max_pages=cfg.book_retain_max_pages,
                max_chars=cfg.book_retain_max_chars,
            )
            store_document_text(
                doc_id,
                kept_text,
                blocks=kept_blocks,
                full_char_count=len(full_text),
                dry_run=dry_run,
            )

            chunk_offset = existing_chunk_count(doc_id)
            total_added = 0

            for section in sections:
                result = ingest_text_block(
                    doc_id,
                    section["text"],
                    Source.CALIBRE,
                    extra_metadata={
                        "title": book.title,
                        "pass": "fulltext",
                        "section_title": section.get("title", ""),
                        "section_index": section.get("index", 0),
                        **section_page_range(section),
                    },
                    chunk_offset=chunk_offset + total_added,
                    dry_run=dry_run,
                    refresh=False,
                )
                if not result["skipped"]:
                    total_added += result["chunks_added"]

            if total_added == 0:
                stats["skipped"] += 1
            else:
                # The §3.2 gap this closes: hundreds of body chunks and not one
                # of them says what the book is about, and /search collapses to
                # the best single chunk per document.
                total_added += attach_summary_chunk(
                    doc_id,
                    full_text,
                    Source.CALIBRE,
                    title=book.title,
                    dry_run=dry_run,
                    refresh=False,
                )
                # Once, here, rather than once per section: the mean-pool is
                # over every chunk the book has, so every earlier pass computed
                # a value this one supersedes. Deferred blocks
                # above make this call the only thing keeping the embedding
                # current — it must stay on this path.
                if not dry_run:
                    # Function-level, like the one in ``ingest_text_block``:
                    # ingestion reaches clustering lazily so the import cycle
                    # through tag training stays broken at module scope.
                    from pka.clustering.doc_embeddings import refresh_document_embedding

                    refresh_document_embedding(doc_id)
                stats["processed"] += 1
                stats["chunks"] += total_added

        except Exception as exc:
            log.exception("Full-text failed for %s: %s", book.source_id, exc)
            stats["failed"] += 1
            failed = True
        finally:
            tick(progress_key, failed=failed)

    return stats
