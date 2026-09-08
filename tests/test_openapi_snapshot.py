"""The committed OpenAPI snapshot must match the app's live schema.

The frontend's `src/api/types.gen.ts` is generated from
`frontend/src/api/openapi.json`, which `scripts/dump_openapi.py` writes from the
FastAPI app. Nothing at runtime notices when a pydantic model changes and the
snapshot does not, so this is the check that does — the whole point of audit
item M-10, which found 48 hand-written interfaces mirroring 39 models with no
gate keeping the two copies honest.

No server is started: `app.openapi()` builds the document in-process.
"""

import json

import pytest

from scripts.dump_openapi import SNAPSHOT, schema, serialize

REGENERATE = (
    "Run:  python scripts/dump_openapi.py && (cd frontend && npm run gen:api)\n"
    "then commit both frontend/src/api/openapi.json and src/api/types.gen.ts."
)


@pytest.fixture(scope="module")
def live() -> dict:
    return schema()


def test_the_snapshot_file_exists():
    assert SNAPSHOT.exists(), f"{SNAPSHOT} is missing. {REGENERATE}"


def test_the_snapshot_matches_the_live_schema(live):
    committed = SNAPSHOT.read_text(encoding="utf-8")
    assert committed == serialize(live), (
        f"The API schema has changed but {SNAPSHOT.name} has not.\n{REGENERATE}"
    )


def test_every_response_model_reaches_the_generated_types(live):
    """A schema the frontend cannot see is a model it will hand-mirror instead."""
    generated = (SNAPSHOT.parent / "types.gen.ts").read_text(encoding="utf-8")
    missing = [name for name in live["components"]["schemas"] if f"{name}:" not in generated]
    assert not missing, f"absent from types.gen.ts: {missing}\n{REGENERATE}"


def test_the_snapshot_is_serialised_stably():
    """Sorted keys and a trailing newline, so a regeneration diffs cleanly."""
    raw = SNAPSHOT.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert raw == serialize(json.loads(raw))
