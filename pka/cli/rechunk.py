"""Re-cut retained document text with the current chunker settings.

What ``document_texts`` was retained for: a chunker or embedding-model change
can be applied to documents already in the archive, with no re-fetch and no
re-extraction. Only documents whose body text was retained are candidates —
there is no backfill, so anything ingested before retention shipped is
untouched and stays that way until something re-fetches it.

Usage::

    alexandria rechunk --dry-run
    alexandria rechunk --source firefox
    alexandria rechunk --limit 100
"""

from __future__ import annotations

import argparse
import logging

from pka.cli._logging import setup_logging
from pka.constants import ALL_SOURCES

log = logging.getLogger("rechunk")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="alexandria rechunk",
        description="Re-chunk documents from their retained text, without re-fetching.",
    )
    parser.add_argument(
        "--source",
        choices=ALL_SOURCES,
        help="Limit to one source connector (default: the whole archive)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Stop after this many documents",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report how many documents have retained text, and change nothing",
    )
    args = parser.parse_args(argv)

    setup_logging()

    from pka.db.queries import init_db
    from pka.ingestion.rechunk import rechunk_documents

    init_db()
    stats = rechunk_documents(source=args.source, dry_run=args.dry_run, limit=args.limit)

    where = args.source or "the whole archive"
    if args.dry_run:
        log.info("%d document(s) with retained text in %s", stats["candidates"], where)
        return 0
    log.info(
        "Re-chunked %d/%d document(s) in %s: +%d chunks, -%d chunks, %d vector(s) purged"
        " (%d skipped)",
        stats["rechunked"],
        stats["candidates"],
        where,
        stats["chunks_added"],
        stats["chunks_removed"],
        stats["vectors_purged"],
        stats["skipped"],
    )
    if stats["skipped_truncated"]:
        log.info(
            "%d book(s) skipped: their retained text is only the first pages"
            " (ALEXANDRIA_BOOK_RETAIN_MAX_PAGES). Re-run the full-text pass to"
            " re-chunk those from the file.",
            stats["skipped_truncated"],
        )
    return 0
