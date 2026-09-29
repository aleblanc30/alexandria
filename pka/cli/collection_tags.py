"""Derive collection tags from the collections already in the archive.

Zotero collections and Firefox bookmark folders become ``collection`` tags
(DESIGN.md §3.7). Ingestion writes them for the documents it touches; this
applies them to everything already archived, from ``source_collections``
alone, with no source database and no network. Re-running it converges, and
with ``collection_tags_enabled`` off it removes them.

Usage::

    alexandria collection-tags --dry-run
    alexandria collection-tags --source firefox
    alexandria collection-tags
"""

from __future__ import annotations

import argparse
import logging

from pka.cli._logging import setup_logging

log = logging.getLogger("collection-tags")


def main(argv: list[str] | None = None) -> int:
    from pka.ingestion.collection_tags import TAGGED_SOURCES

    parser = argparse.ArgumentParser(
        prog="alexandria collection-tags",
        description="Tag documents with their Zotero collections and Firefox folders.",
    )
    parser.add_argument(
        "--source",
        choices=[str(s) for s in TAGGED_SOURCES],
        help="Limit to one source (default: both)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the tags it would write, the most used ones and those over the"
        " document cap, and change nothing",
    )
    args = parser.parse_args(argv)

    setup_logging()

    from pka.db.migrate import init_db
    from pka.ingestion.collection_tags import backfill_collection_tags

    init_db()
    stats = backfill_collection_tags(args.source, dry_run=args.dry_run)

    if not stats["enabled"]:
        verb = "Would remove" if args.dry_run else "Removed"
        log.info("%s every collection tag: collection_tags_enabled is off.", verb)
        return 0
    verb = "Would tag" if args.dry_run else "Tagged"
    for source, s in stats["sources"].items():
        log.info(
            "%s: %s %d document(s) with %d tag(s), %d distinct",
            source,
            verb,
            s["documents"],
            s["tags"],
            s["distinct"],
        )
        for tag, n in s["top"]:
            log.info("  %6d  %s", n, tag)
    if stats["capped"]:
        log.info(
            "Left out, on more than ALEXANDRIA_COLLECTION_TAG_MAX_DOCUMENTS documents"
            " across both sources:"
        )
        for tag, n in stats["capped"]:
            log.info("  %6d  %s", n, tag)
    return 0
