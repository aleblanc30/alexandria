"""Purge one kind of archive artifact, optionally for a single source.

The precise counterpart to ``purge-source``, which is the Tier-3 nuke. Use this
when swapping a backend: clear what the old model produced without destroying
the fetched text (or the source rows) that producing it again depends on.

Usage::

    alexandria purge --list
    alexandria purge summaries --dry-run
    alexandria purge summaries --source firefox
    alexandria purge image_text
"""

from __future__ import annotations

import argparse
import logging
import sys

from pka.cli._logging import setup_logging
from pka.constants import ALL_SOURCES
from pka.purge import TARGETS, purge_target

log = logging.getLogger("purge")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="alexandria purge",
        description="Remove one kind of archive artifact (summaries, vectors, image text, …).",
    )
    parser.add_argument(
        "target",
        nargs="?",
        choices=sorted(TARGETS),
        help="What to purge",
    )
    parser.add_argument(
        "--source",
        choices=ALL_SOURCES,
        help="Limit the purge to one source connector (default: the whole archive)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report row counts without deleting",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List the available targets and what regenerates each",
    )
    prov = parser.add_argument_group(
        "provenance filters",
        "Narrow to what one backend produced (stamped targets only — see --list)",
    )
    prov.add_argument("--run-id", type=int, help="Only artifacts made by this enrichment run")
    prov.add_argument("--provider", help="Only artifacts made by this provider (e.g. ollama)")
    prov.add_argument("--model", help="Only artifacts made by this resolved model name")
    prov.add_argument(
        "--unknown",
        action="store_true",
        help="Only artifacts with no recorded provenance (made before stamping shipped)",
    )
    parser.add_argument(
        "--runs",
        action="store_true",
        help="List recent enrichment runs (what made what, when, at what cost)",
    )
    args = parser.parse_args(argv)

    setup_logging()

    if args.runs:
        from pka.db.queries import init_db
        from pka.enrichment_runs import list_runs

        init_db()
        rows = list_runs(kind=None, limit=50)
        if not rows:
            log.info("No enrichment runs recorded yet.")
        for row in rows:
            log.info(
                "#%-4d %-16s %-12s %-28s %-9s %d artifact(s), %d call(s), %d chars",
                row["run_id"],
                row["kind"],
                row["provider"] or "—",
                row["model"] or "—",
                row["status"],
                row["artifacts"],
                row["calls"],
                row["chars_sent"],
            )
        return 0

    if args.list:
        for target in TARGETS.values():
            log.info(
                "%-15s tier %d  %s%s (retrigger: %s)",
                target.key,
                target.tier,
                target.label,
                " [stamped]" if target.provenance else "",
                target.retrigger or "none",
            )
        return 0

    if not args.target:
        parser.error("a target is required unless --list or --runs is given")

    from pka.db.queries import init_db

    init_db()
    try:
        counts = purge_target(
            args.target,
            source=args.source,
            dry_run=args.dry_run,
            run_id=args.run_id,
            provider=args.provider,
            model=args.model,
            unknown=args.unknown,
        )
    except ValueError as exc:
        log.error("%s", exc)
        sys.exit(1)

    where = args.source or "the whole archive"
    if args.unknown:
        where += ", unknown provenance only"
    elif args.run_id is not None:
        where += f", run #{args.run_id} only"
    elif args.provider or args.model:
        where += f", {args.provider or 'any provider'} / {args.model or 'any model'} only"
    prefix = "Would delete" if args.dry_run else "Deleted"
    log.info("%s for %s:", args.target, where)
    for name, count in counts.items():
        log.info("  %s %s: %d", prefix, name, count)

    retrigger = TARGETS[args.target].retrigger
    if retrigger and not args.dry_run:
        log.info("Regenerate with: %s", retrigger)
    return 0


if __name__ == "__main__":
    main()
