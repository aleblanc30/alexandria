#!/usr/bin/env python
"""Ingest Firefox bookmarks (metadata + fetch + embed).

Usage::

    python scripts/run_firefox.py
    python scripts/run_firefox.py --metadata-only   # skip fetching
    python scripts/run_firefox.py --fetch-only      # skip re-reading places.sqlite
    python scripts/run_firefox.py --dry-run
    python scripts/run_firefox.py --limit 100       # fetch at most N URLs
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka.connectors.firefox import load_bookmarks
from pka.db.queries import init_db
from pka.ingestion.fetcher import fetch_pending
from pka.ingestion.firefox_sync import sync_firefox
from pka.pipeline import ingest_fetched_texts, ingest_firefox_bookmarks

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

    if not args.fetch_only and not args.metadata_only:
        stats = sync_firefox(
            fetch_limit=args.limit,
            fetch_concurrency=args.concurrency,
            dry_run=args.dry_run,
        )
        log.info("Done: %s", stats)
        return

    if not args.fetch_only:
        log.info("Loading Firefox bookmarks…")
        bookmarks = load_bookmarks()
        log.info("Loaded %d bookmarks", len(bookmarks))
        meta_stats = ingest_firefox_bookmarks(bookmarks, dry_run=args.dry_run)
        log.info("Metadata ingestion: %s", meta_stats)

    if not args.metadata_only:
        log.info("Fetching pending URLs (limit=%d)…", args.limit)
        fetch_stats = asyncio.run(fetch_pending(
            limit       = args.limit,
            concurrency = args.concurrency,
        ))
        log.info(
            "Fetch: fetched=%d  skipped=%d  unfetchable=%d",
            fetch_stats["fetched"], fetch_stats["skipped"],
            fetch_stats["unfetchable"],
        )

        if fetch_stats["texts"] and not args.dry_run:
            log.info(
                "Chunking and embedding %d fetched documents…",
                len(fetch_stats["texts"]),
            )
            embed_stats = ingest_fetched_texts(
                fetch_stats["texts"], dry_run=args.dry_run,
            )
            log.info("Embed: %s", embed_stats)


if __name__ == "__main__":
    main()
