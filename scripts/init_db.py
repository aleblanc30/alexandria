#!/usr/bin/env python
"""Create all SQLite tables and run pending migrations (idempotent).

Thin shim — the implementation lives in pka.cli.init_db (run via
`alexandria init` once installed, or `python scripts/init_db.py` from the repo).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka.cli.init_db import main

if __name__ == "__main__":
    sys.exit(main())
