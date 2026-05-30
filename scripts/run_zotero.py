#!/usr/bin/env python
"""Initialise DB, ingest Zotero library, print stats.

Usage::

    python scripts/run_zotero.py
    python scripts/run_zotero.py --dry-run
    python scripts/run_zotero.py --force-reindex
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka.connectors.zotero import load_items
from pka.db.queries import init_db
from pka.pipeline import ingest_zotero_items

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
log = logging.getLogger("run_zotero")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",       action="store_true",
                        help="Skip embedding and storage writes")
    parser.add_argument("--force-reindex", action="store_true",
                        help="Re-chunk and re-embed all items")
    args = parser.parse_args()

    log.info("Initialising database…")
    init_db()

    log.info("Loading Zotero items…")
    items = load_items()
    log.info("Loaded %d items", len(items))

    log.info("Ingesting (dry_run=%s, skip_existing=%s)…",
             args.dry_run, not args.force_reindex)
    stats = ingest_zotero_items(
        items,
        skip_existing = not args.force_reindex,
        dry_run       = args.dry_run,
    )

    log.info(
        "Done. processed=%d  skipped=%d  failed=%d  chunks=%d",
        stats["processed"], stats["skipped"], stats["failed"], stats["chunks"],
    )


if __name__ == "__main__":
    main()
