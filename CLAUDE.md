# AGENTS.md

Instructions for AI coding agents working in this repository.

## Project overview

**Alexandria v0.2.0** is a local-first research library. It ingests Firefox bookmarks, Zotero papers, Calibre ebooks, and image folders into a single semantically indexed SQLite + ChromaDB archive. Inference runs on-device via Ollama; no data leaves the machine.

This repo is the Python backend + Vue frontend. 
Read **`README.md`** for human-oriented setup and usage. Read **`DESIGN.md`** for architecture, data flow, and how to add a new source connector.

## Repository layout

```
pka/              # Python package (config, pipeline, cli, db, connectors, api, clustering)
scripts/          # thin shims around pka/cli (kept for `python scripts/<name>.py`)
tests/            # pytest suite; conftest.py mocks all external I/O
frontend/         # Vue 3 + Vite + Pinia + TypeScript
README.md
DESIGN.md         # living design notes; initial_design.pdf is the original design doc
pyproject.toml
```

## Setup

All backend commands assume **repo root as `cwd`** and the venv is active.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e '.[dev]'
alexandria init        # or: python scripts/init_db.py

cd frontend && npm install
```

The primary dev environment is Linux/WSL; the repo is also checked out on
Windows, where the same venv steps work (`py -3.12 -m venv .venv`). Keep code
Windows-safe: close SQLite connections and temp files before renaming or
reopening them (Windows locks open files).

System prerequisites: Ollama (chat/vision). OCR runs through the vision model by default; the alternative `ocr_provider=easyocr` backend is a pip dependency (no system binary). Images pass a two-step admission gate (`pka/ingestion/image_gate.py`, on by default): EasyOCR text-coverage ≥ `image_gate_text_coverage_min` **and** a fast gate VLM (`image_gate_vision_*`, default Ollama `moondream`) classifying it as a non-`unknown` category; failures are cached in the `image_rejections` table. Copy `.env.example` to `.env` only when overriding defaults; settings use the `ALEXANDRIA_` env prefix via `pka/config.py`.

## Commands

Run from **repo root** unless noted.

| Task | Command |
|------|---------|
| All tests | `pytest` |
| Tests with coverage | `pytest --cov=pka --cov-report=term-missing` |
| Lint (Python) | `ruff check pka tests scripts` |
| Format (Python) | `ruff format pka tests scripts` |
| Dev (backend+frontend, one command) | `alexandria dev` |
| API (dev, CORS) | `ALEXANDRIA_DEV=1 uvicorn pka.api.main:app --reload --port 8420` |
| Frontend (dev) | `cd frontend && npm run dev` |
| Frontend (build) | `cd frontend && npm run build` |
| Init DB | `alexandria init` (or `python scripts/init_db.py`) |
| Unified CLI | `alexandria --help` (`python -m pka.cli` from a checkout) |

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
| Images | `pka/ingestion/image_pipeline.py`, `pka/ingestion/image_extractor.py`, `pka/ingestion/image_gate.py` |
| API surface | `pka/api/main.py` (router list), matching router + schema modules |
| Frontend views | `frontend/src/router.ts`, relevant store + `api/client.ts` methods |

Adding a new source connector: follow the checklist in **`DESIGN.md` §2** (connector → enum → pipeline → script → API router → test → sidebar).

## Boundaries

- **Local-first / privacy**: do not add cloud APIs, telemetry, or outbound data paths unless explicitly requested.
- **Do not commit** `.env`, real database paths, or user data under `data/`.
- **Do not create git commits or PRs** unless the user asks.
- **Do not edit** `.venv/`, `frontend/dist/`, `pka.egg-info/`, or generated caches.
- Schema changes: update `pka/db/schema.py` and ensure `init_db.py` remains idempotent; add/adjust tests.

## When stuck

1. Search existing tests for the pattern you need—they document expected behavior.
2. Check `CHANGELOG.md` for recent audit notes.
3. Prefer extending existing helpers (`_ingest_text_block`, `conftest` fixtures, `api/client.ts`) over new abstractions.
