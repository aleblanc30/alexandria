"""Zotero document ingestion.

Two embedding passes, like Calibre's: the title + abstract (``pass="metadata"``),
then the attached PDF's full text (``pass="fulltext"``). The metadata chunk is
tagged so that re-chunking or purging the PDF body, which treats untagged
chunks as body, leaves the abstract alone.
"""

from __future__ import annotations

import json
import logging

from pka.classification import classify_document, sync_classification_tags
from pka.config import settings as cfg
from pka.connectors.zotero import (
    ZoteroItem,
    zotero_card_summary,
    zotero_document_url_or_path,
    zotero_embed_text,
    zotero_path,
    zotero_url,
)
from pka.constants import FetchStatus, PdfTextLayer, Source
from pka.db.chunks import document_has_chunks, existing_chunk_count, source_ids_with_chunks
from pka.db.documents import (
    DocumentWrite,
    document_index,
    insert_document_if_new,
    set_fetch_status,
    update_card_summary,
    upsert_document,
)
from pka.db.tags import insert_source_collections, insert_source_tags
from pka.ingestion.arxiv import parse_arxiv_url
from pka.ingestion.book_extractor import extract_book_report, section_page_range
from pka.ingestion.collection_tags import sync_collection_tags
from pka.ingestion.core import ingest_text_block
from pka.ingestion.identifiers import resolve_doi
from pka.ingestion.loops import MetadataOutcome, run_embed_loop, run_metadata_loop
from pka.ingestion.progress import should_stop, tick
from pka.ingestion.text_store import section_blocks, store_document_text, truncate_blocks

log = logging.getLogger(__name__)


def _sync_zotero_classification(doc_id: int, item: ZoteroItem) -> None:
    tags = classify_document(
        Source.ZOTERO,
        item_type=item.item_type,
        url_or_path=zotero_document_url_or_path(zotero_url(item), zotero_path(item)),
    )
    sync_classification_tags(doc_id, tags)


def _sync_zotero_card_summary(doc_id: int, item: ZoteroItem, *, dry_run: bool) -> None:
    if dry_run:
        return
    update_card_summary(doc_id, zotero_card_summary(item))


def _zotero_document_write(item: ZoteroItem) -> DocumentWrite:
    """Shared column values for inserting/upserting a Zotero document row."""
    url = zotero_url(item)
    path = zotero_path(item)
    arxiv_id = parse_arxiv_url(item.url) if item.url else None
    return DocumentWrite(
        source=Source.ZOTERO,
        source_id=item.source_id,
        title=item.title,
        url_or_path=zotero_document_url_or_path(url, path),
        date_added=item.date_added,
        fetch_status=FetchStatus.AVAILABLE if item.pdf_path else FetchStatus.PENDING,
        zotero_attachment_key=item.pdf_attachment_key,
        item_type=item.item_type,
        doi=resolve_doi(item.doi, arxiv_id),
        arxiv_id=arxiv_id,
        year=item.year,
        authors_json=json.dumps(item.authors) if item.authors else None,
        zotero_url=url,
        zotero_path=path,
    )


def ingest_zotero_items(
    items: list[ZoteroItem],
    skip_existing: bool = True,
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    stats = {"processed": 0, "skipped": 0, "failed": 0, "chunks": 0}

    for item in items:
        if stop := should_stop(progress_key):
            stats["stopped"] = stop
            break
        failed = False
        try:
            doc_id = upsert_document(_zotero_document_write(item))
            insert_source_tags(doc_id, item.tags, source=Source.ZOTERO)
            insert_source_collections(doc_id, item.collections, source=Source.ZOTERO)
            sync_collection_tags(doc_id, item.collections, Source.ZOTERO)
            _sync_zotero_classification(doc_id, item)
            _sync_zotero_card_summary(doc_id, item, dry_run=dry_run)

            if skip_existing and document_has_chunks(doc_id):
                stats["skipped"] += 1
                continue

            result = ingest_text_block(
                doc_id,
                zotero_embed_text(item),
                Source.ZOTERO,
                extra_metadata={"title": item.title, "pass": "metadata"},
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
            tick(progress_key, failed=failed)

    return stats


def ingest_zotero_metadata(
    items: list[ZoteroItem],
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Persist new Zotero items (documents, tags, collections) without embedding."""
    known = document_index(Source.ZOTERO)

    def _persist(item: ZoteroItem) -> MetadataOutcome:
        if dry_run:
            return "dry_run"
        doc_id = insert_document_if_new(_zotero_document_write(item))
        if doc_id is None:
            return "skipped"
        insert_source_tags(doc_id, item.tags, source=Source.ZOTERO)
        insert_source_collections(doc_id, item.collections, source=Source.ZOTERO)
        sync_collection_tags(doc_id, item.collections, Source.ZOTERO)
        _sync_zotero_classification(doc_id, item)
        _sync_zotero_card_summary(doc_id, item, dry_run=dry_run)
        known[item.source_id] = doc_id
        return "processed"

    return run_metadata_loop(
        items,
        known=known,
        get_source_id=lambda i: i.source_id,
        persist=_persist,
        progress_key=progress_key,
    )


def refresh_zotero_collections(items: list[ZoteroItem]) -> int:
    """Rewrite the collections and collection tags of items already archived.

    The metadata loop persists new items only, so without this an item moved
    to another collection, or one archived before collection paths were read,
    would keep what it had when it was first seen. Returns the documents
    rewritten.
    """
    known = document_index(Source.ZOTERO)
    n = 0
    for item in items:
        doc_id = known.get(item.source_id)
        if doc_id is None:
            continue
        insert_source_collections(doc_id, item.collections, source=Source.ZOTERO)
        sync_collection_tags(doc_id, item.collections, Source.ZOTERO)
        n += 1
    return n


def ingest_zotero_embed(
    items: list[ZoteroItem],
    skip_existing: bool = True,
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Embed title + abstract for Zotero items already in the database."""
    doc_ids = document_index(Source.ZOTERO) if skip_existing else {}
    embedded = source_ids_with_chunks(Source.ZOTERO) if skip_existing else set()

    def _should_skip(_item: ZoteroItem) -> bool:
        return False  # skip_existing is applied inside _process, after the card summary

    def _process(item: ZoteroItem) -> tuple[bool, int]:
        doc_id = doc_ids.get(item.source_id)
        if doc_id is None:
            doc_id = upsert_document(_zotero_document_write(item))
            doc_ids[item.source_id] = doc_id
            _sync_zotero_classification(doc_id, item)
        _sync_zotero_card_summary(doc_id, item, dry_run=dry_run)
        if skip_existing and item.source_id in embedded:
            return False, 0
        result = ingest_text_block(
            doc_id,
            zotero_embed_text(item),
            Source.ZOTERO,
            extra_metadata={"title": item.title, "pass": "metadata"},
            min_chars=1,
            dry_run=dry_run,
        )
        if result["skipped"]:
            return False, 0
        embedded.add(item.source_id)
        return True, result["chunks_added"]

    return run_embed_loop(
        items,
        should_skip=_should_skip,
        process=_process,
        progress_key=progress_key,
        on_error_log=lambda item, exc: log.exception(
            "Zotero embed %s failed: %s",
            item.source_id,
            exc,
        ),
    )


def ingest_zotero_fulltext(
    items: list[ZoteroItem],
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Second pass: extract and embed the full text of each item's attached PDF.

    The Zotero counterpart of ``ingest_calibre_fulltext``, and deliberately the
    same shape: text retained in ``document_texts`` before chunking, one block
    per page group carrying its page range, a scan marked ``no_text_layer``
    rather than silently skipped, and one embedding refresh once every block is
    in. No generated summary: a Zotero item already has its abstract.

    Callers pass only items still missing their full text (see
    ``zotero_sync``); this does not re-check, and re-running it on an item
    appends a second copy of its chunks.
    """
    stats = {"processed": 0, "skipped": 0, "failed": 0, "chunks": 0, "no_text_layer": 0}
    known = document_index(Source.ZOTERO)

    for item in items:
        if stop := should_stop(progress_key):
            stats["stopped"] = stop
            break
        failed = False
        try:
            doc_id = known.get(item.source_id)
            if doc_id is None or not item.pdf_path or not item.pdf_path.exists():
                # Not archived yet (the metadata pass runs first), or the
                # attachment is a link to a file that is no longer there.
                stats["skipped"] += 1
                continue

            report = extract_book_report(item.pdf_path)
            if not report.sections:
                if report.status == PdfTextLayer.NONE:
                    log.info(
                        "No text layer in %s (%d pages) — marking %s",
                        item.pdf_path.name,
                        report.page_count,
                        FetchStatus.NO_TEXT_LAYER,
                    )
                    if not dry_run:
                        set_fetch_status(doc_id, FetchStatus.NO_TEXT_LAYER)
                    stats["no_text_layer"] += 1
                stats["skipped"] += 1
                continue

            full_text, full_blocks = section_blocks(report.sections)
            # The book caps: most attachments are papers well under them, but a
            # Zotero library can hold whole books, and the retained text is only
            # ever read from its opening.
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
            for section in report.sections:
                result = ingest_text_block(
                    doc_id,
                    section["text"],
                    Source.ZOTERO,
                    extra_metadata={
                        "title": item.title,
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
                continue
            if not dry_run:
                # Every block above deferred the refresh; this is the only one.
                from pka.clustering.doc_embeddings import refresh_document_embedding

                refresh_document_embedding(doc_id)
            stats["processed"] += 1
            stats["chunks"] += total_added

        except Exception as exc:
            log.exception("Zotero full text failed for %s: %s", item.source_id, exc)
            stats["failed"] += 1
            failed = True
        finally:
            tick(progress_key, failed=failed)

    return stats
