#!/usr/bin/env python
"""Scan an images folder and run all extraction passes.

Thin shim — the implementation lives in pka.cli.images (run via
`alexandria images` once installed, or `python scripts/run_images.py` from the repo).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka.cli.images import main

if __name__ == "__main__":
    sys.exit(main())
