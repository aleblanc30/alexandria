"""Fold duplicate tags: report, propose, review (DESIGN.md §3.8).

Spellings that differ in case, accents or punctuation fold on their own; this
command handles the rest, which are decisions. ``scan`` proposes candidates,
semantic ones by local embedding similarity; nothing folds until ``accept`` or
``merge``.

Usage::

    alexandria dedupe-tags report
    alexandria dedupe-tags scan --dry-run
    alexandria dedupe-tags scan --kind semantic --threshold 0.9
    alexandria dedupe-tags list --state candidate
    alexandria dedupe-tags accept 12
    alexandria dedupe-tags reject 13
    alexandria dedupe-tags merge "ml" "machine learning"
    alexandria dedupe-tags unmerge "ml"
"""

from __future__ import annotations

import argparse
import logging

from pka.cli._logging import setup_logging

log = logging.getLogger("dedupe-tags")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alexandria dedupe-tags", description="Fold duplicate and equivalent tags."
    )
    sub = parser.add_subparsers(dest="action", required=True)
    report = sub.add_parser("report", help="Spellings the normalisation already folds")
    report.add_argument("--limit", type=int, default=50)
    scan = sub.add_parser("scan", help="Propose fold candidates")
    scan.add_argument(
        "--kind",
        action="append",
        choices=["semantic", "morphology", "initialism"],
        help="Repeatable; default: all three",
    )
    scan.add_argument("--threshold", type=float, help="Cosine similarity for semantic pairs")
    scan.add_argument("--dry-run", action="store_true", help="List the pairs, write nothing")
    lst = sub.add_parser("list", help="Candidates, active folds or rejected pairs")
    lst.add_argument("--state", choices=["candidate", "active", "rejected"], default="candidate")
    lst.add_argument("--limit", type=int, default=100)
    for name, text in (("accept", "Accept candidate ID"), ("reject", "Reject candidate ID")):
        p = sub.add_parser(name, help=text)
        p.add_argument("id", type=int)
    merge = sub.add_parser("merge", help="Fold tag ALIAS into tag CANONICAL")
    merge.add_argument("alias")
    merge.add_argument("canonical")
    unmerge = sub.add_parser("unmerge", help="Undo the fold of tag ALIAS")
    unmerge.add_argument("alias")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    setup_logging()

    from pka.db import tag_aliases
    from pka.db.migrate import init_db
    from pka.tag_dedup import KINDS, describe, scan, variant_report

    init_db()
    if args.action == "report":
        groups = variant_report()
        log.info("%d tag(s) are spelled more than one way:", len(groups))
        for g in groups[: args.limit]:
            log.info("  %-11s %5d  %s", g["origin"], g["documents"], " | ".join(g["variants"]))
    elif args.action == "scan":
        stats = scan(tuple(args.kind or KINDS), threshold=args.threshold, dry_run=args.dry_run)
        verb = "Would propose" if args.dry_run else "Proposed"
        log.info(
            "%s %d pair(s) over %d tags: %s",
            verb,
            stats["proposed"],
            stats["tags"],
            stats["by_kind"],
        )
        if args.dry_run:
            for p in stats["pairs"]:
                score = f" {p['score']:.3f}" if p["score"] is not None else ""
                log.info("  %-10s%s  %s -> %s", p["kind"], score, p["alias"], p["canonical"])
    elif args.action == "list":
        for row in describe(tag_aliases.list_aliases(args.state)[: args.limit]):
            score = f" {row['score']:.3f}" if row["score"] is not None else ""
            log.info(
                "  #%-5d %-10s%s  %s (%d) -> %s (%d)",
                row["id"],
                row["kind"],
                score,
                row["alias"]["label"],
                row["alias"]["documents"],
                row["canonical"]["label"],
                row["canonical"]["documents"],
            )
    elif args.action == "accept":
        row = tag_aliases.accept(args.id)
        log.info("Folded %s into %s", row["alias"], row["canonical"])
    elif args.action == "reject":
        if not tag_aliases.reject(args.id):
            log.error("No alias #%d", args.id)
            return 1
    elif args.action == "merge":
        try:
            row = tag_aliases.merge(args.alias, args.canonical)
        except tag_aliases.AliasError as exc:
            log.error("%s", exc)
            return 1
        log.info("Folded %s into %s", row["alias"], row["canonical"])
    elif args.action == "unmerge":
        if not tag_aliases.unmerge(args.alias):
            log.error("%r is not folded into anything", args.alias)
            return 1
    return 0
