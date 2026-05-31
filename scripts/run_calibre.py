#!/usr/bin/env python
"""Ingest a Calibre library via sync jobs (metadata, embed, optional full-text).

Usage::

    python scripts/run_calibre.py
    python scripts/run_calibre.py --fulltext
    python scripts/run_calibre.py --fulltext --max-pages 50
    python scripts/run_calibre.py --dry-run
    python scripts/run_calibre.py --force-reindex
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka.connectors.calibre import load_books
from pka.db.queries import init_db
from pka.ingestion.calibre_sync import sync_calibre, sync_calibre_metadata
from pka.ingestion.runners.calibre import ingest_calibre_books

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
log = logging.getLogger("run_calibre")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--library",       type=Path, default=None,
                        help="Path to Calibre library folder (overrides config)")
    parser.add_argument("--fulltext",      action="store_true",
                        help="Run full-text extraction after metadata pass")
    parser.add_argument("--max-pages",     type=int, default=None,
                        help="Limit PDF pages per book during full-text pass")
    parser.add_argument("--dry-run",       action="store_true")
    parser.add_argument("--force-reindex", action="store_true",
                        help="Re-embed even books that already have chunks")
    args = parser.parse_args()

    log.info("Initialising database…")
    init_db()

    log.info("Loading Calibre library…")
    books = load_books(library_root=args.library)
    log.info("Loaded %d books", len(books))

    if args.fulltext:
        log.info("Full sync (metadata + embed + fulltext)…")
        stats = sync_calibre(
            dry_run=args.dry_run,
            max_pages=args.max_pages,
        )
        log.info("Done: %s", stats)
        return

    log.info("Metadata registration…")
    meta = sync_calibre_metadata(dry_run=args.dry_run)
    log.info("Metadata: %s", meta.get("metadata", meta))

    log.info("Embedding title + description…")
    s1 = ingest_calibre_books(
        books,
        skip_existing=not args.force_reindex,
        dry_run=args.dry_run,
    )
    log.info(
        "Metadata embed: processed=%d  skipped=%d  failed=%d  chunks=%d",
        s1["processed"], s1["skipped"], s1["failed"], s1["chunks"],
    )


if __name__ == "__main__":
    main()
