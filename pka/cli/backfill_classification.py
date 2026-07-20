"""Backfill item_type and inferred classification tags on existing documents.

Usage::

    alexandria backfill-classification
    alexandria backfill-classification --zotero-only
    alexandria backfill-classification --firefox-only
"""
from __future__ import annotations

import argparse
import logging

import sqlalchemy as sa

from pka.classification import classify_document, sync_classification_tags
from pka.cli._logging import setup_logging
from pka.connectors.zotero import load_items
from pka.constants import Source
from pka.db.queries import get_engine, init_db, update_document_item_type
from pka.db.schema import documents

log = logging.getLogger("backfill_classification")


def _classify_row(doc_id: int, source: str, item_type: str | None, url_or_path: str | None) -> None:
    tags = classify_document(source, item_type=item_type, url_or_path=url_or_path)
    sync_classification_tags(doc_id, tags)


def backfill_zotero_item_types() -> int:
    """Load Zotero connector and update item_type on archived rows."""
    items = load_items()
    updated = 0
    for item in items:
        n = update_document_item_type(Source.ZOTERO, item.source_id, item.item_type)
        updated += n
    log.info("Updated item_type on %d Zotero document(s)", updated)
    return updated


def backfill_classification(*, zotero: bool = True, firefox: bool = True) -> int:
    """Re-run classification tags for archived documents."""
    sources = []
    if zotero:
        sources.append(str(Source.ZOTERO))
    if firefox:
        sources.append(str(Source.FIREFOX))
    if not sources:
        return 0

    eng = get_engine()
    classified = 0
    with eng.connect() as con:
        rows = con.execute(
            sa.select(
                documents.c.id,
                documents.c.source,
                documents.c.item_type,
                documents.c.url_or_path,
            ).where(documents.c.source.in_(sources))
        ).fetchall()
    for doc_id, source, item_type, url_or_path in rows:
        _classify_row(doc_id, source, item_type, url_or_path)
        classified += 1
    log.info("Synced classification tags on %d document(s)", classified)
    return classified


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alexandria backfill-classification")
    parser.add_argument(
        "--zotero-only",
        action="store_true",
        help="Only backfill Zotero documents",
    )
    parser.add_argument(
        "--firefox-only",
        action="store_true",
        help="Only backfill Firefox bookmarks",
    )
    args = parser.parse_args(argv)

    if args.zotero_only and args.firefox_only:
        parser.error("Choose at most one of --zotero-only and --firefox-only")

    setup_logging()
    init_db()

    do_zotero = not args.firefox_only
    do_firefox = not args.zotero_only

    if do_zotero:
        backfill_zotero_item_types()
    backfill_classification(zotero=do_zotero, firefox=do_firefox)
    return 0


if __name__ == "__main__":
    main()
