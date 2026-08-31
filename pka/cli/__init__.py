"""Unified Alexandria command-line interface.

Installed as the ``alexandria`` console script::

    alexandria init
    alexandria zotero --dry-run
    alexandria firefox --limit 100
    alexandria calibre --fulltext
    alexandria images --search "neural network diagram"
    alexandria youtube --metadata-only
    alexandria clustering --accept
    alexandria domain-report --json
    alexandria purge-source firefox --dry-run
    alexandria purge-cluster-runs --all --dry-run
    alexandria backfill-classification
    alexandria dev

Each subcommand delegates to a ``pka.cli.<module>.main(argv)`` that owns its
own argparse parser; ``scripts/*.py`` remain as thin repo-local shims.
"""

from __future__ import annotations

import importlib
import sys

#: subcommand -> (module under pka.cli, one-line help)
COMMANDS: dict[str, tuple[str, str]] = {
    "init": ("init_db", "Create/upgrade the SQLite archive (idempotent)"),
    "zotero": ("zotero", "Sync the Zotero library"),
    "firefox": ("firefox", "Ingest Firefox bookmarks (metadata + fetch + embed)"),
    "reddit": ("reddit", "Ingest Reddit saved posts (metadata + embed + fetch)"),
    "calibre": ("calibre", "Ingest a Calibre library"),
    "images": ("images", "Scan and index the images folder"),
    "youtube": ("youtube", "Ingest saved YouTube videos (metadata + embed)"),
    "clustering": ("clustering", "Run the clustering pipeline"),
    "domain-report": ("domain_report", "Domain frequency report over ingested URLs"),
    "purge-source": ("purge_source", "Remove archived data for a source"),
    "purge-cluster-runs": ("purge_cluster_runs", "Delete stored clustering runs"),
    "backfill-classification": (
        "backfill_classification",
        "Backfill item types and classification tags",
    ),
    "dev": ("dev", "Run backend + frontend together for local development (opens browser)"),
}


def _print_help() -> None:
    lines = ["usage: alexandria <command> [options]", "", "commands:"]
    width = max(len(name) for name in COMMANDS)
    for name, (_, help_text) in COMMANDS.items():
        lines.append(f"  {name:<{width}}  {help_text}")
    lines.append("")
    lines.append("Run 'alexandria <command> --help' for command options.")
    print("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        _print_help()
        return 0
    command, *rest = args
    entry = COMMANDS.get(command)
    if entry is None:
        print(f"alexandria: unknown command {command!r}", file=sys.stderr)
        _print_help()
        return 2
    module = importlib.import_module(f"pka.cli.{entry[0]}")
    return module.main(rest) or 0


if __name__ == "__main__":  # python -m pka.cli
    sys.exit(main())
