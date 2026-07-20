# Changelog

## Unreleased — maintainability pass (June 2026)

### Correctness fixes
- `accept_run` now deactivates all other runs in the same transaction, so
  accepting an older run actually rolls the active state back (design
  §4.2.2). Accept/reject endpoints reject non-`finished` runs with 409.
- Tag training: `apply_pseudo_labels_model` / `get_queue` no longer recurse
  infinitely (→ 500) on untrainable sessions (single-class labels or missing
  embeddings); they train once then raise 400 / return an empty queue.
  `create_session` returns the previously-dropped `bootstrap_negatives_added`.
- Search pagination: the fulltext branch no longer pre-limits its query, so
  pages past the first are complete and `total` is exact. Semantic fetch
  depth scales with the requested offset. Vector-store failures are logged
  instead of silently swallowed.
- `force=true` on sync endpoints now cancels and joins the running worker
  before starting a new one (previously two jobs could run concurrently);
  the frontend surfaces the conflict instead of silently force-retrying.
- `sync_progress.snapshot()` serializes under the lock — it was mutating
  shared phase state while worker threads advanced it.
- Windows portability: `sqlite3` connections are now explicitly closed
  (`contextlib.closing`) in all connectors and the snapshot copier, and the
  remote-PDF temp file is closed before re-opening by name. This fixes 41
  test failures on Windows (file-lock `PermissionError`s).
- `overlay_tags` gained a unique index on (document_id, tag, origin) with a
  dedupe migration — the manual-tag `INSERT OR IGNORE` previously never
  ignored anything, accumulating duplicates.
- `n_noise` in `/runs` responses is computed (chunked docs without an L1
  assignment) instead of hardcoded 0.

### Performance
- Batched the per-row query loops in image hit resolution, cluster tag
  application, tag-training label upserts, `assign_new_docs` (now reuses the
  batched `_doc_mean_embeddings`), and `compute_drift`.
- `insert_overlay_tags` is the single shared overlay-tag write path
  (cluster tags, manual tags, learned tags).

### Tooling
- New unified CLI: `alexandria <command>` (`pka/cli/`), replacing the six
  broken `[project.scripts]` entries that pointed at the non-packaged
  `scripts/` directory; `scripts/*.py` remain as thin shims.
- `.vscode/tasks.json` paths fixed (stale `pka_v0.2.0` subdirectory).
- Tests: mock Chroma now ranks by real embedding distance; shared
  `empty_vector_store` fixture; sync-progress state reset is an autouse
  fixture; new coverage for `image_hits`, `document_serialize`,
  `json_utils`, `sync_helpers`, the CLI dispatcher, and regression tests
  for every fix above.

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
  `ALEXANDRIA_DEV=1` — production serves frontend same-origin.
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
