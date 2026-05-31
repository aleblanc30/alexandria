#!/usr/bin/env python
"""Initialise DB, sync Zotero library, print stats.

Usage::

    python scripts/run_zotero.py
    python scripts/run_zotero.py --dry-run
    python scripts/run_zotero.py --metadata-only
    python scripts/run_zotero.py --force-reindex
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka.db.queries import init_db
from pka.ingestion.zotero_sync import (
    sync_zotero,
    sync_zotero_ingest,
    sync_zotero_metadata,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
log = logging.getLogger("run_zotero")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",       action="store_true",
                        help="Skip embedding and storage writes")
    parser.add_argument("--metadata-only", action="store_true",
                        help="Register items only (no embedding)")
    parser.add_argument("--force-reindex", action="store_true",
                        help="Re-chunk and re-embed all items")
    args = parser.parse_args()

    log.info("Initialising database…")
    init_db()

    log.info("Syncing Zotero (dry_run=%s)…", args.dry_run)
    if args.metadata_only:
        stats = sync_zotero_metadata(dry_run=args.dry_run)
    elif args.force_reindex:
        meta = sync_zotero_metadata(dry_run=args.dry_run)
        embed = sync_zotero_ingest(
            dry_run=args.dry_run,
            skip_existing=False,
        )
        stats = {**meta, **embed}
    else:
        stats = sync_zotero(dry_run=args.dry_run)

    log.info("Done: %s", stats)


if __name__ == "__main__":
    main()
