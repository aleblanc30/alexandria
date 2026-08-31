"""Ingest Firefox bookmarks (metadata + fetch + embed).

Usage::

    alexandria firefox
    alexandria firefox --metadata-only   # skip fetching
    alexandria firefox --fetch-only      # skip metadata import
    alexandria firefox --dry-run
    alexandria firefox --limit 100       # fetch at most N URLs
"""

from __future__ import annotations

import argparse
import logging

from pka.cli._logging import setup_logging
from pka.db.queries import init_db
from pka.ingestion.firefox_sync import (
    sync_firefox,
    sync_firefox_ingest,
    sync_firefox_metadata,
)

log = logging.getLogger("run_firefox")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alexandria firefox")
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--fetch-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=500, help="Max URLs to fetch per run")
    parser.add_argument("--concurrency", type=int, default=5)
    args = parser.parse_args(argv)

    setup_logging()
    log.info("Initialising database…")
    init_db()

    if args.metadata_only:
        stats = sync_firefox_metadata(dry_run=args.dry_run)
        log.info("Metadata: %s", stats)
        return 0

    if args.fetch_only:
        stats = sync_firefox_ingest(
            fetch_limit=args.limit,
            fetch_concurrency=args.concurrency,
            dry_run=args.dry_run,
        )
        log.info("Ingest: %s", stats)
        return 0

    stats = sync_firefox(
        fetch_limit=args.limit,
        fetch_concurrency=args.concurrency,
        dry_run=args.dry_run,
    )
    log.info("Done: %s", stats)
    return 0


if __name__ == "__main__":
    main()
