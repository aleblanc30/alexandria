#!/usr/bin/env python
"""Run the clustering pipeline and print diagnostics.

Thin shim — the implementation lives in pka.cli.clustering (run via
`alexandria clustering` once installed, or `python scripts/run_clustering.py` from the repo).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka.cli.clustering import main

if __name__ == "__main__":
    sys.exit(main())
