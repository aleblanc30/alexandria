#!/usr/bin/env python
"""Ingest Firefox bookmarks (metadata + fetch + embed).

Usage::

    python scripts/run_firefox.py
    python scripts/run_firefox.py --metadata-only   # skip fetching
    python scripts/run_firefox.py --fetch-only      # skip metadata import
    python scripts/run_firefox.py --dry-run
    python scripts/run_firefox.py --limit 100       # fetch at most N URLs
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka.db.queries import init_db
from pka.ingestion.firefox_sync import (
    sync_firefox,
    sync_firefox_ingest,
    sync_firefox_metadata,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
log = logging.getLogger("run_firefox")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--fetch-only",    action="store_true")
    parser.add_argument("--dry-run",       action="store_true")
    parser.add_argument("--limit",         type=int, default=500,
                        help="Max URLs to fetch per run")
    parser.add_argument("--concurrency",   type=int, default=5)
    args = parser.parse_args()

    log.info("Initialising database…")
    init_db()

    if args.metadata_only:
        stats = sync_firefox_metadata(dry_run=args.dry_run)
        log.info("Metadata: %s", stats)
        return

    if args.fetch_only:
        stats = sync_firefox_ingest(
            fetch_limit=args.limit,
            fetch_concurrency=args.concurrency,
            dry_run=args.dry_run,
        )
        log.info("Ingest: %s", stats)
        return

    stats = sync_firefox(
        fetch_limit=args.limit,
        fetch_concurrency=args.concurrency,
        dry_run=args.dry_run,
    )
    log.info("Done: %s", stats)


if __name__ == "__main__":
    main()
