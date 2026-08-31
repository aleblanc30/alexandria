"""Domain frequency report over ingested HTTP(S) URLs.

Usage::

    alexandria domain-report
    alexandria domain-report --source firefox --limit 50
    alexandria domain-report --rejected
    alexandria domain-report --json
"""

from __future__ import annotations

import argparse
import json
import sys

from pka.constants import ALL_SOURCES
from pka.db.queries import init_db
from pka.domains import build_domain_frequency_report


def _format_status(by_fetch_status: dict[str, int]) -> str:
    if not by_fetch_status:
        return ""
    parts = [f"{status}={count}" for status, count in sorted(by_fetch_status.items())]
    return " ".join(parts)


def _print_table(rows: list[dict]) -> None:
    for rank, row in enumerate(rows, start=1):
        handler = "yes" if row["has_handler"] else "no"
        status_part = _format_status(row.get("by_fetch_status") or {})
        line = f"{rank:4}.  {row['domain']:<28} {row['count']:>6}  handler={handler}"
        if status_part:
            line += f"  {status_part}"
        print(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="alexandria domain-report",
        description="List domains from ingested items, sorted by frequency.",
    )
    parser.add_argument(
        "--source",
        choices=list(ALL_SOURCES),
        help="Limit to one source connector",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Show only the top N domains",
    )
    parser.add_argument(
        "--rejected",
        action="store_true",
        help="Sort by unfetchable count instead of document count",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a human-readable table",
    )
    args = parser.parse_args(argv)

    init_db()
    if args.rejected:
        rows = build_domain_frequency_report(source=args.source)
        rows = sorted(
            (r for r in rows if r["unfetchable"] > 0),
            key=lambda r: (-r["unfetchable"], r["domain"]),
        )
        rows = rows[: args.limit]
    else:
        rows = build_domain_frequency_report(source=args.source, limit=args.limit)

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    if not rows:
        scope = f"source={args.source}" if args.source else "all sources"
        print(f"No HTTP(S) URLs found in archive ({scope}).", file=sys.stderr)
        return 0

    _print_table(rows)
    return 0


if __name__ == "__main__":
    main()
