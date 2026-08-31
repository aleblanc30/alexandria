"""Zotero sync — metadata and embed as separate jobs."""
import json
import logging

from pka.connectors.zotero import (
    ensure_zotero_copy,
    load_item_keys,
    load_items,
    zotero_document_url_or_path,
    zotero_path,
    zotero_url,
)
from pka.constants import Source
from pka.db.queries import refresh_zotero_metadata, source_ids_with_chunks
from pka.ingestion import progress as sp
from pka.ingestion.arxiv import parse_arxiv_url
from pka.ingestion.dev_limits import take
from pka.ingestion.identifiers import resolve_doi
from pka.ingestion.pending_metadata import archive_document_count, count_pending_metadata
from pka.ingestion.runners.zotero import ingest_zotero_embed, ingest_zotero_metadata
from pka.ingestion.sync_shared import run_full_sync

log = logging.getLogger(__name__)


def _plan_counts(key: str, total: int) -> None:
    """Set shared corpus size for all phases (Firefox ingest pattern)."""
    sp.set_corpus_total(key, total)


def _load_zotero_items_for_embed(skip_existing: bool = True) -> tuple[list, int, int]:
    """Load only items that still need embedding; return (items, total, skipped)."""
    dst = ensure_zotero_copy()
    all_keys = set(take(sorted(load_item_keys(copy_path=dst, skip_copy=True)), Source.ZOTERO))
    total = len(all_keys)
    if skip_existing:
        embedded = source_ids_with_chunks(Source.ZOTERO)
        pending = all_keys - embedded
        skipped = total - len(pending)
        if not pending:
            return [], total, skipped
        items = load_items(copy_path=dst, skip_copy=True, keys=pending)
        return items, total, skipped
    return take(load_items(copy_path=dst, skip_copy=True), Source.ZOTERO), total, 0


def sync_zotero_metadata(
    progress_key: str | None = None,
    dry_run: bool = False,
) -> dict:
    from pka.db.queries import init_db

    init_db()
    key = progress_key or "zotero"
    baseline = archive_document_count(Source.ZOTERO)
    pending = count_pending_metadata(Source.ZOTERO)
    sp.begin_metadata_sync(key, pending, baseline)
    items = take(load_items(), Source.ZOTERO)
    stats = ingest_zotero_metadata(items, dry_run=dry_run, progress_key=key)
    if not dry_run:
        by_source_id = {}
        for i in items:
            url = zotero_url(i)
            path = zotero_path(i)
            arxiv_id = parse_arxiv_url(i.url) if i.url else None
            by_source_id[i.source_id] = {
                "zotero_attachment_key": i.pdf_attachment_key,
                "doi": resolve_doi(i.doi, arxiv_id),
                "arxiv_id": arxiv_id,
                "year": i.year,
                "authors_json": json.dumps(i.authors) if i.authors else None,
                "zotero_url": url,
                "zotero_path": path,
                "url_or_path": zotero_document_url_or_path(url, path),
            }
        n_refreshed = refresh_zotero_metadata(by_source_id)
        if n_refreshed:
            log.info("Zotero metadata refreshed on %d row(s)", n_refreshed)
    log.info("Zotero metadata: %s", stats)
    return {"metadata": stats, "stopped": stats.get("stopped")}


def sync_zotero_ingest(
    progress_key: str | None = None,
    dry_run: bool = False,
    skip_existing: bool = True,
) -> dict:
    key = progress_key or "zotero"
    items, n, pre_skipped = _load_zotero_items_for_embed(skip_existing=skip_existing)
    _plan_counts(key, n)
    sp.skip_phase(key, "fetching")
    sp.set_phase(key, "embedding", n)
    if not items:
        stats = {"processed": 0, "skipped": pre_skipped, "failed": 0, "chunks": 0}
        log.info("Zotero embed: nothing to do (%d already embedded)", pre_skipped)
        return {"embed": stats}
    stats = ingest_zotero_embed(
        items,
        skip_existing=skip_existing,
        dry_run=dry_run,
        progress_key=key,
    )
    stats["skipped"] += pre_skipped
    log.info("Zotero embed: %s", stats)
    return {"embed": stats, "stopped": stats.get("stopped")}


def sync_zotero(
    progress_key: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Full sync (metadata + embed). Kept for scripts/tests."""
    meta = sync_zotero_metadata(progress_key=progress_key, dry_run=dry_run)
    return run_full_sync(
        meta, lambda: sync_zotero_ingest(progress_key=progress_key, dry_run=dry_run),
    )
