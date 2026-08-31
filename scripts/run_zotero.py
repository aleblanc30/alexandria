#!/usr/bin/env python
"""Initialise DB, sync Zotero library, print stats.

Thin shim — the implementation lives in pka.cli.zotero (run via
`alexandria zotero` once installed, or `python scripts/run_zotero.py` from the repo).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka.cli.zotero import main

if __name__ == "__main__":
    sys.exit(main())
