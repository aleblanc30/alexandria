"""Ingest saved YouTube videos (metadata import + embed).

Requires the optional ``youtube`` extra and OAuth credentials::

    pip install -e '.[youtube]'
    export ALEXANDRIA_YOUTUBE_CLIENT_SECRET=/path/to/client_secret.json

Usage::

    alexandria youtube
    alexandria youtube --metadata-only   # import rows, skip embedding
    alexandria youtube --embed-only       # embed already-imported videos
    alexandria youtube --dry-run
"""

from __future__ import annotations

import argparse
import logging

from pka.cli._logging import setup_logging
from pka.db.queries import init_db
from pka.ingestion.youtube_sync import (
    sync_youtube,
    sync_youtube_ingest,
    sync_youtube_metadata,
)

log = logging.getLogger("run_youtube")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alexandria youtube")
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--embed-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    setup_logging()
    log.info("Initialising database…")
    init_db()

    if args.metadata_only:
        stats = sync_youtube_metadata(dry_run=args.dry_run)
        log.info("Metadata: %s", stats)
        return 0

    if args.embed_only:
        stats = sync_youtube_ingest(dry_run=args.dry_run)
        log.info("Embed: %s", stats)
        return 0

    stats = sync_youtube(dry_run=args.dry_run)
    log.info("Done: %s", stats)
    return 0


if __name__ == "__main__":
    main()
