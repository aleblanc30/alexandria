#!/usr/bin/env python
"""Ingest Firefox bookmarks (metadata + fetch + embed).

Thin shim — the implementation lives in pka.cli.firefox (run via
`alexandria firefox` once installed, or `python scripts/run_firefox.py` from the repo).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka.cli.firefox import main

if __name__ == "__main__":
    sys.exit(main())
