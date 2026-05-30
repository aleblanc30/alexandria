# PKA Design Notes

The authoritative design specification for the Personal Knowledge Archive is
the PDF design document (kept separately by the author). This file holds
supplementary notes accumulated during implementation and the v0.2.0 audit
pass, organised so they can be cross-referenced from the code.

If the PDF is added to the source tree, place it at `docs/design.pdf` and
treat its statements as overriding anything written below.

## 1. Data flow

```mermaid
graph TD
    A[Firefox places.sqlite] --> D[Source Connectors]
    B[Zotero zotero.sqlite]  --> D
    C[Calibre metadata.db]   --> D
    I[Image folder]          --> D
    D --> E[Chunker]
    E --> F[Ollama Embedder]
    F --> G[ChromaDB]
    F --> H[SQLite archive.db]
    G --> J[Clustering Engine]
    H --> J
    J --> K[FastAPI]
    K --> L[Vue Frontend]
```

## 2. Adding a new source connector

To add a new source (e.g. Pocket, Raindrop, Readwise):

1. Create `pka/connectors/<source>.py` exposing a `load_items()` function
   that returns a list of dataclass objects with at minimum the fields
   `source_id`, `title`, `tags`, `date_added`.

2. Add the source to the `Source` enum in `pka/constants.py`.

3. Add `ingest_<source>_items()` to `pka/pipeline.py`, routing text through
   `_ingest_text_block()` (the helper handles chunking, embedding, and
   persistence uniformly).

4. Add `scripts/run_<source>.py` as the CLI entry point.

5. Register the source name in `pka/api/routers/ingestion.py::_sync` so the
   `/ingestion/sync/{source}` endpoint can fan out to it.

6. Add a fixture in `tests/conftest.py` and a test module
   `tests/test_connector_<source>.py`.

7. Add an entry to the sidebar in `frontend/src/components/AppSidebar.vue`.

## 3. Two-phase ingestion model

Calibre and Firefox follow a two-phase pattern:

- **Phase 1** is fast and deterministic. It writes document rows and
  embeds whatever cheap text is immediately available (title + abstract
  for Zotero, title + description for Calibre, bookmark metadata for
  Firefox). Phase 1 is what every routine `python scripts/run_*.py`
  performs.

- **Phase 2** is slow and side-effecting. It pulls full-text from PDFs/EPUBs
  (Calibre) or fetches and extracts HTML (Firefox) and embeds the result.
  Chunk indices are offset past the phase-1 chunks via `existing_chunk_count()`
  so the two passes coexist in a single document.

Phase-2 work is gated behind `--fulltext` (Calibre) or runs asynchronously
through `pka.ingestion.fetcher.fetch_pending()` (Firefox).

## 4. Cluster lifecycle

Every clustering run is stored regardless of acceptance. The UI surfaces
runs through `/runs` and lets the operator accept exactly one as the active
run. Drift detection (`compute_drift`) and merge suggestions
(`compute_merge_suggestions`) operate against the active run and flag
clusters that may need to be split or merged, but never act automatically.
