#!/usr/bin/env python
"""Remove all archived data for a source connector.

Thin shim — the implementation lives in pka.cli.purge_source (run via
`alexandria purge-source` once installed, or `python scripts/purge_source.py` from the repo).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka.cli.purge_source import main

if __name__ == "__main__":
    sys.exit(main())
