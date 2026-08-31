#!/usr/bin/env python
"""Domain frequency report over ingested HTTP(S) URLs.

Thin shim — the implementation lives in pka.cli.domain_report (run via
`alexandria domain-report` once installed, or `python scripts/domain_report.py` from the repo).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pka.cli.domain_report import main

if __name__ == "__main__":
    sys.exit(main())
