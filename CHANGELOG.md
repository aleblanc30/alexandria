# Changelog

## v0.2.0 — audit pass

### Critical correctness fixes
- `documents.ingested_at` column added; drift detection now uses it instead
  of `date_added` (which is the source-side timestamp and can be much older
  than the actual ingestion time).
- Zotero `_FIELD_NAMES` changed from a `set` to a `tuple` — fixes
  non-deterministic SQL parameter binding when the set iteration order
  differs from the placeholder order.
- LLM JSON parsing uses a regex-based fence stripper instead of the
  misused `str.strip(seq)` call (which removes any character in the
  argument string, not a prefix).
- CLIP Chroma collection is now cached at module scope (was creating a new
  client on every image).
- Firefox folder path reconstruction made iterative; no recursion, no cycle
  risk.

### Performance
- Search result assembly reduced from N+1 queries to four batched lookups
  in `_batch_doc_rows_to_out`.
- Clustering loads embeddings in 5000-vector pages instead of pulling the
  full collection into memory.

### Architecture
- `Source` and `FetchStatus` enums (in `pka/constants.py`) replace the bare
  strings sprinkled throughout the codebase.
- `_ingest_text_block()` is the single chunking → embedding → persistence
  path; the five ingestion functions are now thin wrappers around it.
- Sentence splitter has an optional spaCy backend, with a regex fallback
  that protects common abbreviations.
- CORS middleware is only mounted when the backend is started with
  `PKA_DEV=1` — production serves frontend same-origin.
- Path validator on the settings model rejects `/etc`, `/usr`, `/var`,
  `/sys` roots.
- `cluster_runs.umap_points` JSON column persists 2-D UMAP coordinates so
  the scatter endpoint returns real geometry instead of placeholder
  randomness.

### Frontend
- `api/client.ts` adds a 30-second timeout, `AbortController`, and an
  `ApiError` class with a status field.
- Toast store + container surface API errors at the bottom-right of the
  viewport with auto-dismiss.
- `ScatterPlot.vue` axis bounds moved to a component-scoped reactive ref,
  and the canvas is wrapped in a fixed-height container so re-renders no
  longer inflate the y-axis.
- `RunManagerView.vue` exposes diagnostics through a `computed()` so the
  template updates when the store changes.

### Tests
- New: `test_rate_limiter.py` — verifies same-domain throttling and
  cross-domain concurrency.
- New: `test_pipeline_calibre_integration.py` — verifies chunk-index
  offsets across the metadata and full-text passes.
- `conftest.py` resets the cached CLIP client/collection between tests.

### Documentation
- `README.md` covers setup, run, and test workflows.
- `DESIGN.md` placeholder records that the PDF design document is the
  authoritative source.
- This file.
