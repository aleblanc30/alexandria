"""Zotero document ingestion."""
from __future__ import annotations

import logging

from pka.card_summary import zotero_card_summary
from pka.classification import classify_document, sync_classification_tags
from pka.connectors.zotero import ZoteroItem, zotero_document_url_or_path, zotero_embed_text
from pka.constants import FetchStatus, Source
from pka.db.queries import (
    document_has_chunks,
    document_index,
    insert_document_if_new,
    insert_source_collections,
    insert_source_tags,
    source_ids_with_chunks,
    update_card_summary,
    upsert_document,
)
from pka.ingestion.core import ingest_text_block
from pka.ingestion.loops import MetadataOutcome, run_embed_loop, run_metadata_loop
from pka.ingestion.runners._common import progress_tick, stop_requested

log = logging.getLogger(__name__)


def _sync_zotero_classification(doc_id: int, item: ZoteroItem) -> None:
    tags = classify_document(
        Source.ZOTERO,
        item_type=item.item_type,
        url_or_path=zotero_document_url_or_path(item),
    )
    sync_classification_tags(doc_id, tags)


def _sync_zotero_card_summary(doc_id: int, item: ZoteroItem, *, dry_run: bool) -> None:
    if dry_run:
        return
    update_card_summary(doc_id, zotero_card_summary(item))


def _zotero_document_kwargs(item: ZoteroItem) -> dict:
    """Shared column values for inserting/upserting a Zotero document row."""
    return dict(
        source=Source.ZOTERO,
        source_id=item.source_id,
        title=item.title,
        url_or_path=zotero_document_url_or_path(item),
        date_added=item.date_added,
        fetch_status=FetchStatus.AVAILABLE if item.pdf_path else FetchStatus.PENDING,
        zotero_attachment_key=item.pdf_attachment_key,
        item_type=item.item_type,
    )


def ingest_zotero_items(
    items: list[ZoteroItem],
    skip_existing: bool = True,
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    stats = {"processed": 0, "skipped": 0, "failed": 0, "chunks": 0}

    for item in items:
        if (stop := stop_requested(progress_key)):
            stats["stopped"] = stop
            break
        failed = False
        try:
            doc_id = upsert_document(**_zotero_document_kwargs(item))
            insert_source_tags(doc_id, item.tags, source=Source.ZOTERO)
            insert_source_collections(doc_id, item.collections, source=Source.ZOTERO)
            _sync_zotero_classification(doc_id, item)
            _sync_zotero_card_summary(doc_id, item, dry_run=dry_run)

            if skip_existing and document_has_chunks(doc_id):
                stats["skipped"] += 1
                continue

            result = ingest_text_block(
                doc_id, zotero_embed_text(item), Source.ZOTERO,
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
            progress_tick(progress_key, failed=failed)

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
        doc_id = insert_document_if_new(**_zotero_document_kwargs(item))
        if doc_id is None:
            return "skipped"
        insert_source_tags(doc_id, item.tags, source=Source.ZOTERO)
        insert_source_collections(doc_id, item.collections, source=Source.ZOTERO)
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


def ingest_zotero_embed(
    items: list[ZoteroItem],
    skip_existing: bool = True,
    dry_run: bool = False,
    progress_key: str | None = None,
) -> dict:
    """Embed title + abstract for Zotero items already in the database."""
    doc_ids = document_index(Source.ZOTERO) if skip_existing else {}
    embedded = source_ids_with_chunks(Source.ZOTERO) if skip_existing else set()

    def _should_skip(item: ZoteroItem) -> bool:
        return False

    def _process(item: ZoteroItem) -> tuple[bool, int]:
        doc_id = doc_ids.get(item.source_id)
        if doc_id is None:
            doc_id = upsert_document(**_zotero_document_kwargs(item))
            doc_ids[item.source_id] = doc_id
            _sync_zotero_classification(doc_id, item)
        _sync_zotero_card_summary(doc_id, item, dry_run=dry_run)
        if skip_existing and item.source_id in embedded:
            return False, 0
        result = ingest_text_block(
            doc_id, zotero_embed_text(item), Source.ZOTERO,
            extra_metadata={"title": item.title},
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
            "Zotero embed %s failed: %s", item.source_id, exc,
        ),
    )
