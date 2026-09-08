"""Write the FastAPI schema to the snapshot the frontend types are generated from.

The frontend used to hand-mirror every pydantic model as a TypeScript interface,
so a schema change had to be made twice and nothing checked the two copies still
agreed (planning audit M-10). The snapshot this writes is the single source both
sides read: ``npm run gen:api`` turns it into ``types.gen.ts``, and
``tests/test_openapi_snapshot.py`` fails when the app's schema drifts from it.

Run after changing any request or response model::

    python scripts/dump_openapi.py && (cd frontend && npm run gen:api)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parent.parent / "frontend" / "src" / "api" / "openapi.json"


def schema() -> dict:
    """The app's OpenAPI document, built in-process — no server needed."""
    from pka.api.main import app

    return app.openapi()


def serialize(doc: dict) -> str:
    """Stable rendering: sorted keys, two-space indent, trailing newline."""
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def main() -> int:
    text = serialize(schema())
    if SNAPSHOT.exists() and SNAPSHOT.read_text(encoding="utf-8") == text:
        print(f"{SNAPSHOT} already current")
        return 0
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(text, encoding="utf-8")
    print(f"wrote {SNAPSHOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
