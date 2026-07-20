#!/usr/bin/env python
"""Remove clustering run rows and related cluster data.

Thin shim — the implementation lives in pka.cli.purge_cluster_runs (run via
`alexandria purge-cluster-runs` once installed, or `python scripts/purge_cluster_runs.py` from the repo).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka.cli.purge_cluster_runs import main

if __name__ == "__main__":
    sys.exit(main())
