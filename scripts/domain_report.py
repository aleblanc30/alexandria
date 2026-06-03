#!/usr/bin/env python
"""Domain frequency report over ingested HTTP(S) URLs.

Usage::

    python scripts/domain_report.py
    python scripts/domain_report.py --source firefox --limit 50
    python scripts/domain_report.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


def main() -> None:
    parser = argparse.ArgumentParser(
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
        "--json",
        action="store_true",
        help="Emit JSON instead of a human-readable table",
    )
    args = parser.parse_args()

    init_db()
    rows = build_domain_frequency_report(source=args.source, limit=args.limit)

    if args.json:
        print(json.dumps(rows, indent=2))
        return

    if not rows:
        scope = f"source={args.source}" if args.source else "all sources"
        print(f"No HTTP(S) URLs found in archive ({scope}).", file=sys.stderr)
        return

    _print_table(rows)


if __name__ == "__main__":
    main()
