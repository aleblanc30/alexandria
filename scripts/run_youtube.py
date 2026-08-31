#!/usr/bin/env python
"""Ingest saved YouTube videos (metadata + embed).

Thin shim — the implementation lives in pka.cli.youtube (run via
`alexandria youtube` once installed, or `python scripts/run_youtube.py` from the repo).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka.cli.youtube import main

if __name__ == "__main__":
    sys.exit(main())
