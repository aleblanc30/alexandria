# Alexandria — v0.2.0

This project is entirely coded by LLM agents. It is an experiment to learn how to use those.

A local-first research library that unifies a Firefox bookmark collection, a
Zotero academic library, a Calibre ebook collection, and an unstructured
images folder into a single semantically indexed, searchable knowledge base.

All inference runs on-device via [Ollama](https://ollama.com). No data ever
leaves the machine.

## Repository layout

```
pka/                          # Python backend
├── config.py                 # Pydantic settings (ALEXANDRIA_ env prefix, .env supported)
├── constants.py              # Source, FetchStatus, TagOrigin enums
├── pipeline.py               # _ingest_text_block + per-source ingestion funcs
├── cli/                      # `alexandria` CLI (init, sync, clustering, purge, …)
├── db/                       # SQLAlchemy Core schema + queries
├── connectors/               # zotero, firefox, calibre, images
├── ingestion/                # chunker, fetcher, extractors, image_pipeline
├── storage/                  # ChromaDB wrapper
├── clustering/               # UMAP + HDBSCAN engine + lifecycle (drift/merge)
└── api/                      # FastAPI app, 9 routers, 5 Pydantic schema modules

scripts/                      # thin shims around pka/cli for repo-local runs
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
alexandria init            # or: python scripts/init_db.py

# Frontend
cd frontend
npm install
```

Installing the package provides the `alexandria` console command (also
runnable as `python -m pka.cli`). The `scripts/` files are thin shims kept
for `python scripts/<name>.py` workflows; both forms run the same code.

System prerequisites:

- **Ollama** for clustering labels and image vision (`ollama pull llava` or your
  chat model). Text chunk embeddings use Chroma's built-in Sentence Transformers
  model (`all-MiniLM-L6-v2`, downloaded on first use).
- **Ollama Cloud** (optional) runs bigger models than the machine fits, without
  the prompts touching a third-party aggregator. Two routes, both per-capability
  so chat can go remote while OCR and embeddings stay local:
  - *Through the local daemon* — `ollama signin`, `ollama pull gpt-oss:120b-cloud`,
    then set `ALEXANDRIA_CHAT_MODEL=gpt-oss:120b-cloud`. Provider stays `ollama`.
  - *Direct to ollama.com* — set `ALEXANDRIA_CHAT_PROVIDER=ollama_cloud` (and/or
    `_VISION_PROVIDER`), an API key from https://ollama.com/settings/keys as
    `SECRET_ALEXANDRIA_OLLAMA_CLOUD_API_KEY`, and
    `ALEXANDRIA_OLLAMA_CLOUD_CHAT_MODEL` (no `-cloud` suffix on this route). No
    local daemon needed.
- **Image OCR** runs through the vision model by default (`ocr_provider=vlm`,
  no extra install). Set `ocr_provider=easyocr` to use the bundled **EasyOCR**
  backend instead — a pip dependency (no system binary); its recognition models
  download on first use.
- **Image admission gate** (on by default) filters incoming images before the
  expensive passes: an image is kept only if EasyOCR finds it is at least 5%
  text *and* a fast VLM (Ollama `moondream` by default) classifies it into a
  category of interest. Rejected paths are cached in the `image_rejections`
  table and skipped on later runs. Tune with `ALEXANDRIA_IMAGE_GATE_*` (see
  `.env.example`) or bypass per-run with `alexandria images --skip-gate`.

## Running

### Ingest sources

```bash
alexandria zotero
alexandria firefox                  # metadata + async fetch + embed
alexandria calibre --fulltext
alexandria images
```

Common flags across the subcommands: `--dry-run`, `--force-reindex`. See
`alexandria <command> --help` for the full set.

```bash
alexandria domain-report            # domains by frequency (prioritize fetch handlers)
alexandria domain-report --source firefox --limit 50
```

### Cluster and review

```bash
alexandria clustering               # creates a stored, unaccepted run
alexandria clustering --accept      # accept it immediately
alexandria clustering --drift       # drift report on the active run
alexandria clustering --merges      # merge candidates on the active run
alexandria clustering --assign-new  # assign new docs to existing clusters
```

### Serve the API and frontend

```bash
# Dev: one command starts both, streams both logs, and opens the browser
alexandria dev
```

This runs `uvicorn --reload` (port 8420) and `npm run dev` (port 5173)
together in one terminal and opens `http://localhost:5173` once the frontend
is ready. Ctrl+C (or closing the window) stops both, and if either server
doesn't come up within 15s (e.g. the port is already taken by something
else) the command reports that and shuts everything down instead of leaving
a half-working stack running. On Windows, double-clicking
[`scripts/run_dev.bat`](scripts/run_dev.bat) does the same — useful as the
target of a Desktop shortcut.

Equivalent manual two-terminal form, if you want the processes' logs kept
separate:

```bash
# Terminal 1: backend with CORS enabled (frontend on :5173 talks to backend on :8420)
ALEXANDRIA_DEV=1 uvicorn pka.api.main:app --reload --port 8420

# Terminal 2
cd frontend && npm run dev
```

```bash
# Production: build the frontend, drop CORS, mount /dist at the API root
cd frontend && npm run build
uvicorn pka.api.main:app --port 8420
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

All settings can be overridden via the `ALEXANDRIA_` environment-variable prefix or
a `.env` file. See `.env.example` for the full list.

## Design

The initial design document is committed as [`initial_design.pdf`](initial_design.pdf)
(parts are outdated, but it remains the reference for intent). `DESIGN.md`
contains the living supplementary notes (data flow diagram, instructions
for adding a new source connector). The audit pass that produced v0.2.0 is
recorded in `CHANGELOG.md`.

Note on naming: the Python package is `pka` (Personal Knowledge Archive, the
project's original name); "Alexandria" is the user-facing brand. The
`ALEXANDRIA_` env prefix and the `alexandria` CLI follow the brand.
