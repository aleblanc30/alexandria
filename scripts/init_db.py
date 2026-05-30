#!/usr/bin/env python
"""Create all SQLite tables and run any pending in-place migrations.

This is idempotent and safe to run on an existing database.
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka.db.queries import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)

if __name__ == "__main__":
    init_db()
    logging.getLogger("init_db").info("Database ready.")
