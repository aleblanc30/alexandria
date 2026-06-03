# AGENTS.md

Instructions for AI coding agents working in this repository.

## Project overview

**Alexandria v0.2.0** is a local-first research library. It ingests Firefox bookmarks, Zotero papers, Calibre ebooks, and image folders into a single semantically indexed SQLite + ChromaDB archive. Inference runs on-device via Ollama; no data leaves the machine.

This repo is the Python backend + Vue frontend. The parent workspace (`../`) may also hold shared Cursor config (`.cursor/`) and VS Code tasks (`.vscode/`).

Read **`README.md`** for human-oriented setup and usage. Read **`DESIGN.md`** for architecture, data flow, and how to add a new source connector.

## Repository layout

```
pka/              # Python package (config, pipeline, db, connectors, api, clustering)
scripts/          # CLI entry points (run_zotero, run_clustering, init_db, …)
tests/            # pytest suite (~150 tests); conftest.py mocks all external I/O
frontend/         # Vue 3 + Vite + Pinia + TypeScript
README.md
DESIGN.md
pyproject.toml
```

## Setup

All backend commands assume **repo root as `cwd`** and the venv is active.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
python scripts/init_db.py

cd frontend && npm install
```

System prerequisites: Ollama (chat/vision), Tesseract OCR (images). Copy `.env.example` to `.env` only when overriding defaults; settings use the `ALEXANDRIA_` env prefix via `pka/config.py`.

## Commands

Run from **repo root** unless noted.

| Task | Command |
|------|---------|
| All tests | `pytest` |
| Tests with coverage | `pytest --cov=pka --cov-report=term-missing` |
| Lint (Python) | `ruff check pka tests scripts` |
| Format (Python) | `ruff format pka tests scripts` |
| API (dev, CORS) | `ALEXANDRIA_DEV=1 uvicorn pka.api.main:app --reload --port 8000` |
| Frontend (dev) | `cd frontend && npm run dev` |
| Frontend (build) | `cd frontend && npm run build` |
| Init DB | `python scripts/init_db.py` |

The parent workspace VS Code task **"alexandria: dev stack"** starts API + frontend in parallel.

Do **not** run full ingestion scripts (`run_zotero`, `run_firefox`, etc.) or production uvicorn during routine agent work unless the user asks—they touch real source databases and external services.

## Testing rules

- Tests must stay **fully isolated**: `tests/conftest.py` redirects all data paths to `tmp_path` and mocks Ollama, ChromaDB, HTTP, and CLIP. Never remove or bypass these mocks.
- Add fixtures to `conftest.py` for new external boundaries; keep test modules focused (`test_<area>.py`).
- Run `pytest` after backend changes. Run `npm run build` in `frontend/` after TypeScript/Vue changes.
- Only add tests that cover real behavior; skip trivial assertions.

## Code conventions

### Python (`pka/`, `scripts/`, `tests/`)

- Python **3.11+**. Line length **100** (ruff). Enabled rules: E, F, I, B, UP.
- Settings: `pka/config.py` (Pydantic). Enums: `pka/constants.py` (`Source`, `FetchStatus`, `TagOrigin` are string enums).
- DB: SQLAlchemy Core in `pka/db/schema.py` + queries in `pka/db/queries.py`. No ORM models.
- Ingestion flows through `pka/ingestion/core.py` (`ingest_text_block`) and `pka/ingestion/runners/`; `pka/pipeline.py` re-exports for compatibility.
- API: FastAPI routers in `pka/api/routers/`, Pydantic schemas in `pka/api/schemas/`. App entry: `pka/api/main.py`.
- Match existing module layout and naming. Keep diffs minimal; do not refactor unrelated code.

### Frontend (`frontend/src/`)

- Vue 3 composition API, Pinia stores, typed API client in `api/client.ts`.
- Views in `views/`, shared UI in `components/`, global styles in `styles/global.css`.
- API base URL and error handling live in `client.ts`—extend there rather than ad-hoc fetch calls.

## Architecture pointers

Before modifying an area, skim these entry points:

| Area | Start here |
|------|------------|
| Ingestion / connectors | `pka/ingestion/core.py`, `pka/ingestion/runners/`, `pka/connectors/`, `DESIGN.md` §2–3 |
| Search / vectors | `pka/storage/vector_store.py`, `pka/api/routers/search.py` |
| Clustering | `pka/clustering/engine.py`, `pka/clustering/lifecycle.py`, `scripts/run_clustering.py` |
| Images | `pka/ingestion/image_pipeline.py`, `pka/ingestion/image_extractor.py` |
| API surface | `pka/api/main.py` (router list), matching router + schema modules |
| Frontend views | `frontend/src/router.ts`, relevant store + `api/client.ts` methods |

Adding a new source connector: follow the checklist in **`DESIGN.md` §2** (connector → enum → pipeline → script → API router → test → sidebar).

## Boundaries

- **Local-first / privacy**: do not add cloud APIs, telemetry, or outbound data paths unless explicitly requested.
- **Do not commit** `.env`, real database paths, or user data under `data/`.
- **Do not create git commits or PRs** unless the user asks.
- **Do not edit** `.venv/`, `frontend/dist/`, `pka.egg-info/`, or generated caches.
- Schema changes: update `pka/db/schema.py` and ensure `init_db.py` remains idempotent; add/adjust tests.

## Agent task board

The parent workspace may track high-level tasks in `../.cursor/agent-board/tasks.json`. When working on a board task (prompt tagged `[Agent Board task …]`):

1. Move the task to `in_progress` at start.
2. Keep `agent-board.canvas.data.json` in sync with `tasks.json`.
3. Move to `review` when done (or `done` if the user requests it).

Details: `../.cursor/rules/agent-board.mdc`.

## When stuck

1. Search existing tests for the pattern you need—they document expected behavior.
2. Check `CHANGELOG.md` for recent audit notes.
3. Prefer extending existing helpers (`_ingest_text_block`, `conftest` fixtures, `api/client.ts`) over new abstractions.
