"""Find and link duplicate documents (DESIGN.md §3.9).

``scan`` links documents sharing a DOI, arXiv id, ISBN or canonical URL, and
proposes near duplicates by document embedding for review. Links hide the
duplicate behind its canonical without deleting anything; ``reject`` undoes one.

Usage::

    alexandria dedupe scan --dry-run
    alexandria dedupe scan
    alexandria dedupe scan --no-embeddings
    alexandria dedupe list --state candidate
    alexandria dedupe accept 12
    alexandria dedupe reject 13
    alexandria dedupe link 101 202        # 202 is a duplicate of 101
"""

from __future__ import annotations

import argparse
import logging

from pka.cli._logging import setup_logging

log = logging.getLogger("dedupe")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alexandria dedupe", description="Find and link duplicate documents."
    )
    sub = parser.add_subparsers(dest="action", required=True)
    scan = sub.add_parser("scan", help="Link exact duplicates; propose near ones")
    scan.add_argument("--no-embeddings", action="store_true", help="Exact keys only")
    scan.add_argument("--threshold", type=float, help="Cosine similarity for near duplicates")
    scan.add_argument("--dry-run", action="store_true", help="List what it would do")
    lst = sub.add_parser("list", help="Links, proposals or rejected pairs")
    lst.add_argument("--state", choices=["candidate", "merged", "rejected"], default="candidate")
    lst.add_argument("--limit", type=int, default=100)
    for name, text in (("accept", "Link candidate ID"), ("reject", "Reject or undo link ID")):
        p = sub.add_parser(name, help=text)
        p.add_argument("id", type=int)
    link = sub.add_parser("link", help="Link DUPLICATE_ID into CANONICAL_ID")
    link.add_argument("canonical_id", type=int)
    link.add_argument("duplicate_id", type=int)
    return parser


def _pair(row: dict) -> str:
    c, d = row["canonical"], row["duplicate"]
    return f"[{d['source']}] {d['title'][:60]!r}  ->  [{c['source']}] {c['title'][:60]!r}"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    setup_logging()

    from pka.db import duplicates
    from pka.db.migrate import init_db
    from pka.dedupe import describe, scan

    init_db()
    if args.action == "scan":
        stats = scan(
            embeddings=not args.no_embeddings, threshold=args.threshold, dry_run=args.dry_run
        )
        verb = "Would link" if args.dry_run else "Linked"
        log.info("%s %d duplicate(s) %s", verb, stats["linked"], stats["by_key"])
        log.info(
            "%s %d near duplicate(s) for review",
            "Would propose" if args.dry_run else "Proposed",
            stats["proposed"],
        )
        if args.dry_run:
            for row in describe([{**r, "id": 0, "state": "merged"} for r in stats["links"]]):
                log.info("  %-8s %s", row["match_key"], _pair(row))
            for row in describe(
                [{**r, "id": 0, "state": "candidate"} for r in stats["candidates"]]
            ):
                log.info("  %.3f    %s", row["score"], _pair(row))
    elif args.action == "list":
        for row in describe(duplicates.list_links(args.state)[: args.limit]):
            how = f"{row['score']:.3f}" if row["score"] is not None else row["match_key"]
            log.info("  #%-5d %-8s %s", row["id"], how, _pair(row))
    elif args.action == "accept":
        duplicates.accept(args.id)
    elif args.action == "reject":
        if not duplicates.reject(args.id):
            log.error("No link #%d", args.id)
            return 1
    elif args.action == "link":
        try:
            duplicates.link(args.canonical_id, args.duplicate_id)
        except duplicates.LinkError as exc:
            log.error("%s", exc)
            return 1
    return 0
