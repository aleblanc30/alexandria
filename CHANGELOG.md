# Changelog

## Unreleased — image admission gate

- New **two-step gate** in front of the image pipeline
  (`pka/ingestion/image_gate.py`), on by default (`image_gate_enabled`). An
  image is only carried into the expensive describe/OCR/CLIP passes when it
  clears **both**: (1) EasyOCR-measured text coverage ≥
  `image_gate_text_coverage_min` (default `0.05`), and (2) a fast VLM classifies
  it into a non-`unknown` category of interest. The cheap local coverage check
  runs first; the VLM is only called if it passes.
- The gate classifier is a **distinct, configurable backend**
  (`image_gate_vision_provider` / `image_gate_vision_model`, default Ollama
  `moondream`) — separate from the main `vision_model`, which re-classifies
  independently later in the pipeline. Remote (OpenRouter/OVH) gate models are
  supported via a dedicated `get_gate_vision_provider()` accessor that bakes the
  gate model in (the OpenAI-compatible provider lets its constructor model win).
- Rejections are cached in a new **`image_rejections`** table (path, reason,
  coverage, type). A rejected image now **leaves no rows behind**:
  `delete_image_document` drops the `images` + backing `documents` row that the
  metadata pass registered (and purges chunk/CLIP vectors if it had been fully
  ingested). Both `register_images` (metadata sync) and `ingest_images` skip
  cached rejects on later runs — no re-registration, no re-gating.
- **Deferred display:** images are hidden from browse until fully ingested
  (`indexed_at` set). The document browse list (`_exclude_pending_images`) and
  the `/images` gallery both filter out registered-but-not-yet-embedded images,
  so the panel never shows half-processed items.
- EasyOCR gained `text_coverage()`; the gate uses it directly, independent of
  `ocr_provider`, so `easyocr` (a core dep) is required when the gate is on.
  CLI: `alexandria images --skip-gate`; env: `ALEXANDRIA_IMAGE_GATE_*`.
- **Rejection cache is now cleared with the images.** Purging the image source
  empties `image_rejections` (previously it lingered, so the metadata pass kept
  skipping purged paths forever). New `alexandria images --reset-rejections`
  clears the cache without a full purge, for re-tuning gate thresholds.
- **EXIF orientation handling.** Portrait phone photos (`Orientation=6`) are now
  transposed upright before both EasyOCR (`_oriented_rgb_array`) and the vision
  encoder (`_encode_image`). Previously EasyOCR crashed on them
  (`cv2.resize !ssize.empty()`), scoring `0.0` coverage and wrongly rejecting
  every such image; the VLM also saw them sideways.
- **Gate failures now surface instead of silently rejecting.** A missing EasyOCR
  install raises `EasyOcrUnavailable` (checked via the real wheel, not the lazy
  wrapper import); a vision-backend outage raises `VisionUnavailable` (gate calls
  the classifier in `strict=True` mode). Both come back as *failed* (retryable),
  never *rejected*, so a broken backend can no longer poison the cache for a whole
  library. A genuine `unknown` verdict from a working VLM still rejects as before.

## Unreleased — YouTube saved-videos connector

- New source connector: **YouTube saved videos** (`Source.YOUTUBE`). Reads the
  authenticated user's playlists (Liked videos + all owned playlists) via the
  **YouTube Data API v3** and ingests one document per unique video. A video in
  multiple playlists collapses to one document (each playlist recorded as a
  source collection; earliest save time wins). Content is **metadata only** —
  title, channel, description, and the video's own tags embed as the searchable
  text; every video also gets an inferred `video` browse tag. Transcript-based
  enrichment is deferred (`BACKLOG.md`).
- **Deliberate cloud exception:** Alexandria stays local-first everywhere else.
  This is the one sanctioned outbound integration and is inert unless
  `ALEXANDRIA_YOUTUBE_CLIENT_SECRET` (a desktop-app OAuth client) is set. Scope
  is read-only (`youtube.readonly`); the refresh token is cached under `data/`
  and never committed. Google client libraries are an opt-in extra
  (`pip install -e '.[youtube]'`) and are lazy-imported, so the connector module
  stays importable/testable without them.
- CLI: `alexandria youtube [--metadata-only|--embed-only|--dry-run]`
  (`scripts/run_youtube.py`). Sidebar gains a YouTube source; its configurable
  "path" is the OAuth client-secret JSON. Status polling never touches the API
  (network-free pending/corpus counts).

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
