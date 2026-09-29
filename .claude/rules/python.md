---
paths:
  - "pka/**/*.py"
  - "scripts/**/*.py"
---

# Python conventions (`pka/`, `scripts/`)

Python **3.11+**.

## ruff

`ruff check pka tests scripts` / `ruff format pka tests scripts`.

- `line-length = 100`, but **`E501` is ignored** — the formatter owns wrapping.
  Do not hand-wrap a long line that ruff has not flagged.
- Rules selected: `E`, `F`, `I`, `B`, `UP`.
- `pka/api/routers/*` ignores **`B008`**: FastAPI's `Depends(...)` / `Query(...)`
  in argument defaults triggers it by design. Not a bug to fix.

## Module layout

| Concern | Location |
|---------|----------|
| Settings | `pka/config.py` (Pydantic, `ALEXANDRIA_` env prefix) |
| Enums | `pka/constants.py` (`Source`, `FetchStatus`, `TagOrigin` are string enums) |
| DB | SQLAlchemy **Core** in `pka/db/schema.py`; engine in `pka/db/engine.py`, migrations in `pka/db/migrate.py`, queries in one module per aggregate (`documents`, `chunks`, `cards`, `tags`, `browse`, `clusters`, `reddit`, `images`) — no ORM models. `pka/db/queries.py` is a deprecated re-export shim |
| Ingestion | `pka/ingestion/core.py` (`ingest_text_block`) + `pka/ingestion/runners/` |
| Model backends | `pka/providers/` — every LLM/vision/OCR call goes through a provider, never a backend directly |
| API | routers in `pka/api/routers/`, schemas in `pka/api/schemas/`, app in `pka/api/main.py` |

## Deprecated module

`pka/pipeline.py` is **deprecated** and exists only as a back-compat re-export
shim. Import from `pka.ingestion.core` / `pka.ingestion.runners` instead. In
particular the underscore alias `_ingest_text_block` is the old name for
`ingest_text_block`; do not use it in new code.

## Schema changes

Update `pka/db/schema.py` and keep `init_db` **idempotent** — `alexandria init`
must be safe to re-run against an existing archive. A column or index added to a
table that already shipped also needs a step appended to `MIGRATIONS` in
`pka/db/migrate.py`, under a new name; never rename, reorder or edit a shipped
step, because archives that recorded it will not run it again. Add the column or
index to `tests/test_schema_migration.py`'s lists alongside.
