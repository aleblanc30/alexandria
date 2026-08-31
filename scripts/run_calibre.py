#!/usr/bin/env python
"""Ingest a Calibre library via sync jobs.

Thin shim — the implementation lives in pka.cli.calibre (run via
`alexandria calibre` once installed, or `python scripts/run_calibre.py` from the repo).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka.cli.calibre import main

if __name__ == "__main__":
    sys.exit(main())
