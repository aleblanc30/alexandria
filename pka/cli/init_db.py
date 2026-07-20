"""Create all SQLite tables and run any pending in-place migrations.

This is idempotent and safe to run on an existing database.
"""
from __future__ import annotations

import logging

from pka.cli._logging import setup_logging
from pka.db.queries import init_db


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    init_db()
    logging.getLogger("init_db").info("Database ready.")
    return 0


if __name__ == "__main__":
    main()
