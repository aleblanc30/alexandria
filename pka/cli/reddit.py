"""Ingest Reddit saved posts (metadata + embed + fetch).

Requires the saved-links feed URL from https://www.reddit.com/prefs/feeds/ in
``SECRET_ALEXANDRIA_REDDIT_FEED_URL``::

    alexandria reddit
    alexandria reddit --backfill        # walk the whole feed, not just what is new
    alexandria reddit --metadata-only   # persist saved list, skip embedding/fetch
    alexandria reddit --ingest-only      # embed inline bodies + fetch link posts
    alexandria reddit --from-archive    # replay data/reddit/saved.jsonl, no polling
    alexandria reddit --dry-run

Every poll is written to ``data/reddit/<timestamp>/`` (raw Atom pages plus a
manifest) and merged into ``data/reddit/saved.jsonl``; ``--from-archive`` reads
that log back when the feed is no longer reachable.

An ordinary run reads it too: anything the log holds that the database does not
is ingested off disk before the walk, and counts as already-seen for it, so the
feed is polled only for what neither store has.
"""

from __future__ import annotations

import argparse
import logging

from pka.cli._logging import setup_logging
from pka.db.queries import init_db
from pka.ingestion.reddit_sync import (
    sync_reddit,
    sync_reddit_ingest,
    sync_reddit_metadata,
)

log = logging.getLogger("run_reddit")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alexandria reddit")
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--ingest-only", action="store_true")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Walk the whole feed instead of stopping at the "
        "first already-saved item (first run, or to fill "
        "gaps a failed run left)",
    )
    parser.add_argument(
        "--from-archive",
        action="store_true",
        help="Replay data/reddit/saved.jsonl instead of polling "
        "the feed (recovery after a lost database or a "
        "token that no longer works)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    setup_logging()
    log.info("Initialising database…")
    init_db()

    if args.metadata_only:
        stats = sync_reddit_metadata(
            dry_run=args.dry_run,
            backfill=args.backfill,
            from_archive=args.from_archive,
        )
        log.info("Metadata: %s", stats)
        return 0

    if args.ingest_only:
        stats = sync_reddit_ingest(dry_run=args.dry_run, from_archive=args.from_archive)
        log.info("Ingest: %s", stats)
        return 0

    stats = sync_reddit(
        dry_run=args.dry_run,
        backfill=args.backfill,
        from_archive=args.from_archive,
    )
    log.info("Done: %s", stats)
    return 0


if __name__ == "__main__":
    main()
