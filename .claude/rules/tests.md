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

## What to test

Only add tests that cover real behavior. Skip trivial assertions. Existing tests
document expected behavior — search them for a pattern before inventing one.
