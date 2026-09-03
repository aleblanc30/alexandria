# Alexandria — v0.0.8

This project is entirely coded by LLM agents. It is an experiment to learn how to use those.

A local-first research library that unifies a Firefox bookmark collection, a
Zotero academic library, a Calibre ebook collection, and an unstructured
images folder into a single semantically indexed, searchable knowledge base.

Inference runs through swappable providers — on-device via
[Ollama](https://ollama.com) by default, or against a hosted endpoint when the
local machine cannot run a model large enough to be useful.

Alexandria is local-*first*, not local-*only*: the default configuration keeps
your library contents and your queries on the machine, and every outbound path
is a separate named setting that is off until you turn it on. Nothing phones
home, and there is no telemetry or analytics in any configuration. See
[DESIGN.md](DESIGN.md) §1.1 for the network-access policy and what crosses the
wire in each case.

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
└── api/                      # FastAPI app (routers + Pydantic schema modules)

scripts/                      # thin shims around pka/cli for repo-local runs
tests/                        # pytest suite; conftest mocks Ollama, Chroma, HTTP
frontend/                     # Vue 3 + Vite + Pinia
├── index.html
├── package.json, vite.config.ts, tsconfig.json
└── src/
    ├── main.ts, router.ts, App.vue
    ├── api/client.ts         # typed fetch wrappers (timeout + ApiError)
    ├── stores/               # search, clusters, ingestion, ui, toast
    ├── views/                # routed views
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
- **Reddit** reads your saved posts through the private feed URL from
  https://www.reddit.com/prefs/feeds/ — no app registration, which currently
  needs an API-access clearance personal accounts do not get. Paste either the
  RSS or the JSON form into `SECRET_ALEXANDRIA_REDDIT_FEED_URL` in `.secrets`;
  either is normalised to the Atom (`.rss`) endpoint, the only one Reddit serves
  to automated clients. That URL grants read access to your
  saved list: treat it like a password. This feed URL is the only credential the
  connector takes — the OAuth API (`praw`) route has been removed, since the
  "script" app it needs is no longer obtainable. Every poll is archived to
  `data/reddit/<timestamp>/` (the raw feed pages, plus a manifest) and merged
  into `data/reddit/saved.jsonl`, which holds one deduplicated line per saved
  item; `alexandria reddit --from-archive` re-ingests from that log with no
  network access, for when the token dies or the database has to be rebuilt.
  Turn the archive off with `ALEXANDRIA_REDDIT_ARCHIVE_ENABLED=0`.
- **Image OCR** runs through the vision model by default (`ocr_provider=vlm`,
  no extra install). Set `ocr_provider=easyocr` to use the bundled **EasyOCR**
  backend instead — a pip dependency (no system binary); its recognition models
  download on first use.
- **CLIP visual search** is **off by default** (`ALEXANDRIA_CLIP_ENABLED=1` to
  turn it on). With it off, no image embedding pass runs and no CLIP model is
  downloaded; images are still searchable through the text the pipeline infers
  from them (per-type content extraction + description + OCR), which is indexed
  alongside every other document. Turn it on for *purely visual* queries whose
  words appear nowhere in that text. `/images/search` accepts
  `mode=hybrid|clip|text` and reports which path matched each hit.
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
alexandria domain-report --rejected # sorted by unfetchable count instead
```

### Purge and re-run parts of the pipeline

Swapping an embedding or summarisation backend does not mean re-ingesting
everything. `alexandria purge` clears one kind of artifact — leaving the
expensive fetched text, and your own tags and reading lists, in place.

```bash
alexandria purge --list                       # targets, and what regenerates each
alexandria purge summaries --dry-run          # counts first; nothing is deleted
alexandria purge summaries --source firefox   # or scope it to one connector
alexandria purge vectors                      # then POST /ingestion/rebuild-vectors
```

Summaries record which model made them, so swapping a backend does not mean
discarding the work you are keeping:

```bash
alexandria purge --runs                              # what ran, when, at what cost
alexandria purge summaries --model qwen2.5:3b        # only the old model's work
alexandria purge summaries --unknown                 # only the unstamped backlog
```

A target whose artifact carries no stamp rejects those filters rather than
quietly purging everything.

Clearing an artifact is usually the whole re-trigger: the next sync regenerates
whatever is missing. Summaries are the exception — their skip gate is "does this
document have chunks", which stays true — so they get an explicit pass:

```bash
curl -X POST 'localhost:8420/ingestion/enrich?kind=summary'
```

`alexandria purge-source <source>` remains the blunt instrument: it removes a
whole connector's documents. It now keeps manually-applied and learned tags plus
reading-list entries, since re-ingesting cannot recreate those; pass
`--include-user-data` to delete them too.

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

This runs `uvicorn --reload` (port 8421) and `npm run dev` (port 5173)
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
# Terminal 1: backend with CORS enabled (frontend on :5173 talks to backend on :8421)
ALEXANDRIA_DEV=1 uvicorn pka.api.main:app --reload --port 8421

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
a `.env` file. See `.env.example` for the full list. Once the app is running,
`/settings` shows what the current process actually resolved — provider,
model, endpoint and credential presence per capability (with a reachability
check for local/remote backends), plus every other setting grouped and marked
where it differs from its default. It is read-only; `.env` / `.secrets` remain
the way to change anything.

## Design

`DESIGN.md` is the living design specification (data flow, provider layer,
instructions for adding a new source connector). The original design document
is archived at [`docs/archive/initial_design.pdf`](docs/archive/initial_design.pdf);
it records the project's initial intent and is superseded wherever it disagrees
with `DESIGN.md`. The audit pass that produced v0.2.0 is
recorded in `CHANGELOG.md`.

Note on naming: the Python package is `pka` (Personal Knowledge Archive, the
project's original name); "Alexandria" is the user-facing brand. The
`ALEXANDRIA_` env prefix and the `alexandria` CLI follow the brand.
