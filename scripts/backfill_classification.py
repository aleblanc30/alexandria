#!/usr/bin/env python
"""Backfill item_type and classification tags.

Thin shim — the implementation lives in pka.cli.backfill_classification (run via
`alexandria backfill-classification` once installed, or `python scripts/backfill_classification.py` from the repo).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka.cli.backfill_classification import main

if __name__ == "__main__":
    sys.exit(main())
