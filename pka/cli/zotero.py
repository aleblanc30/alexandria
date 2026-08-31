"""Initialise DB, sync Zotero library, print stats.

Usage::

    alexandria zotero
    alexandria zotero --dry-run
    alexandria zotero --metadata-only
    alexandria zotero --force-reindex
"""

from __future__ import annotations

import argparse
import logging

from pka.cli._logging import setup_logging
from pka.db.queries import init_db
from pka.ingestion.zotero_sync import (
    sync_zotero,
    sync_zotero_ingest,
    sync_zotero_metadata,
)

log = logging.getLogger("run_zotero")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alexandria zotero")
    parser.add_argument("--dry-run", action="store_true", help="Skip embedding and storage writes")
    parser.add_argument(
        "--metadata-only", action="store_true", help="Register items only (no embedding)"
    )
    parser.add_argument(
        "--force-reindex", action="store_true", help="Re-chunk and re-embed all items"
    )
    args = parser.parse_args(argv)

    setup_logging()
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
    return 0


if __name__ == "__main__":
    main()
