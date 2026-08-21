---
paths:
  - "tests/**/*.py"
---

# Test conventions (`tests/`)

## Isolation invariant — do not break

`tests/conftest.py` redirects every data path to `tmp_path` and mocks Ollama,
ChromaDB, HTTP, and CLIP. **Never remove or bypass these mocks.** A test that
reaches a real Zotero/Firefox/Calibre database, a real Ollama, or the network is
a broken test, not a thorough one.

Add fixtures to `conftest.py` when you introduce a new external boundary. Keep
test modules focused: `test_<area>.py`.

## pytest configuration

- **`asyncio_mode = "auto"`** — async tests need no `@pytest.mark.asyncio` decorator.
- `addopts = "-v --tb=short"`; `testpaths = ["tests"]`.

## Coverage

`pytest --cov=pka --cov-report=term-missing` enforces **`fail_under = 85`**.
A non-zero exit from that command may mean coverage dipped below 85%, not that
the suite failed — read the summary before treating it as a broken build. Plain
`pytest` does not apply the threshold.

Coverage **omits** `pka/cli/*`, `pka/pipeline.py`, `pka/db/init_db.py`,
`scripts/*`, and `pka/db/alembic/*` — thin wrappers and deprecated shims where
tests are not expected.

## Failing tests — rework, never delete

**Never delete a test as a quick way to get a green suite.** That drops coverage
and makes the software more brittle: the assertion disappears while the behavior
it protected still ships.

When a test fails repeatedly and is hard to fix, reworking it is legitimate in
two cases:

- **The behavior is still tested.** You restructured, moved, or split the
  assertion, but nothing stopped being checked.
- **You are iterating on a new design** and the test asserts an interface that is
  deliberately changing.

Outside those two cases a stubborn failure is a signal to **ask for guidance**,
not to decide alone. Say what is failing and what you already tried — do not
hesitate on this; asking is cheaper than a silently weakened suite.

## What to test

Only add tests that cover real behavior. Skip trivial assertions. Existing tests
document expected behavior — search them for a pattern before inventing one.
