#!/usr/bin/env python
"""Ingest Reddit saved posts (metadata + embed + fetch).

Thin shim — the implementation lives in pka.cli.reddit (run via
`alexandria reddit` once installed, or `python scripts/run_reddit.py` from the repo).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka.cli.reddit import main

if __name__ == "__main__":
    sys.exit(main())
