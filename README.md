# Personal Knowledge Archive (PKA) — v0.2.0

A local-first research library that unifies a Firefox bookmark collection, a
Zotero academic library, a Calibre ebook collection, and an unstructured
images folder into a single semantically indexed, searchable knowledge base.

All inference runs on-device via [Ollama](https://ollama.com). No data ever
leaves the machine.

## Repository layout

```
pka/                          # Python backend
├── config.py                 # Pydantic settings (PKA_ env prefix, .env supported)
├── constants.py              # Source, FetchStatus, TagOrigin enums
├── pipeline.py               # _ingest_text_block + per-source ingestion funcs
├── db/                       # SQLAlchemy Core schema + queries
├── connectors/               # zotero, firefox, calibre, images
├── ingestion/                # chunker, fetcher, extractors, image_pipeline
├── storage/                  # ChromaDB wrapper
├── clustering/               # UMAP + HDBSCAN engine + lifecycle (drift/merge)
└── api/                      # FastAPI app, 9 routers, 5 Pydantic schema modules

scripts/                      # CLI entry points (init_db, run_*, run_clustering)
tests/                        # ~150 pytest tests; conftest mocks Ollama, Chroma, HTTP
frontend/                     # Vue 3 + Vite + Pinia
├── index.html
├── package.json, vite.config.ts, tsconfig.json
└── src/
    ├── main.ts, router.ts, App.vue
    ├── api/client.ts         # typed fetch wrappers (timeout + ApiError)
    ├── stores/               # search, clusters, ingestion, ui, toast
    ├── views/                # 7 routed views
    ├── components/           # AppSidebar, DocCard, DocDetailPanel, ScatterPlot, …
    └── styles/global.css
```

## Setup

```bash
# Python 3.11+
pip install -e .
# Optional extras
pip install -e '.[dev]'    # pytest, ruff, mypy
pip install -e '.[spacy]'  # better sentence splitting

# Database — idempotent, safe to re-run
python scripts/init_db.py

# Frontend
cd frontend
npm install
```

System prerequisites:

- **Ollama** for clustering labels and image vision (`ollama pull llava` or your
  chat model). Text chunk embeddings use Chroma's built-in Sentence Transformers
  model (`all-MiniLM-L6-v2`, downloaded on first use).
- **Tesseract OCR** for image text extraction
  (`brew install tesseract` / `apt install tesseract-ocr`).

## Running

### Ingest sources

```bash
python scripts/run_zotero.py
python scripts/run_firefox.py             # metadata + async fetch + embed
python scripts/run_calibre.py --fulltext
python scripts/run_images.py
```

Common flags across the scripts: `--dry-run`, `--force-reindex`. See
`--help` per script for the full set.

### Cluster and review

```bash
python scripts/run_clustering.py                # creates a stored, unaccepted run
python scripts/run_clustering.py --accept       # accept it immediately
python scripts/run_clustering.py --drift        # drift report on the active run
python scripts/run_clustering.py --merges       # merge candidates on the active run
python scripts/run_clustering.py --assign-new   # assign new docs to existing clusters
```

### Serve the API and frontend

```bash
# Dev: backend with CORS enabled (frontend on :5173 talks to backend on :8000)
PKA_DEV=1 uvicorn pka.api.main:app --reload --port 8000

# In another terminal
cd frontend && npm run dev

# Production: build the frontend, drop CORS, mount /dist at the API root
cd frontend && npm run build
uvicorn pka.api.main:app --port 8000
```

The build output ships to `frontend/dist`. The FastAPI app mounts that
directory at `/` automatically when it exists, so a single uvicorn process
serves both API and UI.

## Tests

```bash
pytest
pytest --cov=pka --cov-report=term-missing
```

All external calls (Ollama chat/vision, ChromaDB, HTTP) are mocked at the module
boundary by fixtures in `tests/conftest.py`. No real services are touched.

Frontend unit tests (Vitest, pure TS helpers):

```bash
cd frontend && npm run test
```

## Configuration

All settings can be overridden via the `PKA_` environment-variable prefix or
a `.env` file. See `.env.example` for the full list.

## Design

The authoritative specification lives in the PDF design document. `DESIGN.md`
in this repository contains supplementary notes (data flow diagram, instructions
for adding a new source connector). The audit pass that produced v0.2.0 is
recorded in `CHANGELOG.md`.
