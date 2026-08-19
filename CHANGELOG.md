# Changelog

## Unreleased — local-first relaxed; retrieval enrichment

- **Ingestion progress rebuilt as a four-layer package and moved onto SSE**
  (`pka/ingestion/progress/`: `state` → `tracker` → `view`/`baselines`). Plan and
  measurements in `sync_refactor.md`.
  - The reported "circular imports" were not cycles — an AST walk over `pka/` finds
    zero. What multiplied the function-local imports was a *layering* cycle: state
    reached down into `pending_metadata` for DB counts while `progress_baselines`
    reached back up into state. With the layers separated, all six deferred imports
    hoist, and `sync_helpers.py` and `runners/_common.py` — two files that existed
    only to hold them — are gone.
  - The stalls were DB I/O under the progress lock: `snapshot()` held it while
    counting archive rows, so every worker's `advance()` queued behind a query
    running twice a second. `snapshot_states()` now copies under the lock and
    serializes outside it, and nothing under `tracker` opens a connection.
  - Workers are the single writer. `run_metadata_loop` ticks each item it actually
    persists instead of leaving metadata progress to be inferred from DB counts at
    serialize time — a metadata sync used to stop advancing when the browser tab
    was closed. Reads stopped mutating: `GET /ingestion/sync/progress` no longer
    hydrates shared state as a side effect, and idle DB counts are applied to the
    copy being serialized.
  - Per-source behaviour is declared, not hardcoded: `PhaseSpec` in `registry.py`
    replaces three `source == FIREFOX` branches. The `fulltext`/`ingesting` phase
    aliases had no production callers left and are gone; `set_phase` now rejects an
    unknown phase name instead of raising `KeyError` deeper in.
  - `GET /ingestion/sync/events?source=` replaces the 500 ms poll loop: one stream
    per running source, coalesced to 5 frames/sec, carrying the per-source slice of
    `/ingestion/status` so nothing polls that endpoint during a sync either. The
    server closes the stream at a terminal status; the store falls back to polling
    if `EventSource` fails mid-job. `/ingestion/status` itself is unchanged — it
    still serves the sidebar and the index metrics.
  - Two missing indexes were doing real damage: `chunks.document_id` and
    `documents.source` were unindexed, so "how many documents are embedded?"
    full-scanned the chunks table at **15.1–15.8 ms** per source per poll (linear in
    chunk count). With them, **0.02–0.37 ms**. Also on that path: `ensure_zotero_copy`
    now reuses a recent snapshot via `ensure_sqlite_copy` instead of backing up the
    whole library inside an HTTP request, and the image probe is one query rather
    than one per image.
  - `tests/test_progress_contract.py` pins the serialized payload for ten
    source/phase/status combinations by full-dict equality; it is what proves the
    rewrite did not move the numbers the UI draws.

- **Every Reddit poll is now archived to disk** (`pka/connectors/reddit_archive.py`,
  `ALEXANDRIA_REDDIT_ARCHIVE_ENABLED`, default on — a local write, not an outbound
  path). Reddit is the one source with no local original to re-read: the saved list
  lives behind a token that can be rotated and a feed that serves only the newest
  slice, so a poll is unrepeatable. Each walk writes its raw Atom pages verbatim to
  `data/reddit/<timestamp>/` with a manifest, and merges its items into a
  cumulative `data/reddit/saved.jsonl`.
  - Incremental without duplicates: a line is appended only when an item is new or
    its content digest changed, so re-running a backfill appends nothing while an
    edited comment appends a new record and wins on read. Digests rather than ids
    alone, because an id-only rule would silently drop edits.
  - Written from a `finally`, so a walk that dies mid-feed keeps the pages it got
    and names the failure in its manifest. Every write is best-effort: an archive
    that took a sync down with it would defeat its own purpose. The feed URL — the
    credential — never reaches the files.
  - `alexandria reddit --from-archive` (and `from_archive=` on the sync functions)
    replays `saved.jsonl` through the pipeline with no network access, for a
    rebuilt database or a token that stopped working.

- **Reddit saved posts can be loaded without an OAuth app**, via the private
  token-bearing feed from `/prefs/feeds/` (`reddit_feed_url`, preferred over
  PRAW when set). Creating a "script" app now requires a separate API-access
  clearance a personal account does not get by default — the create form simply
  re-renders — so the feed is the route that actually works. Verified against a
  live account: 25 items, then 100/page once the loader started asking.
  - One endpoint, `saved.rss`. The richer `saved.json` listing answers automated
    clients with a 403 whose body is the web app's HTML — the token is never
    evaluated, so rotating it changes nothing — and it is no longer requested at
    all: trying it first only opened every sync with a blocked request against
    an account Reddit already watches. A pasted `.json` URL is normalised to
    `.rss`; the token is the same either way.
  - No new dependency: Atom is parsed with `ElementTree` as `arxiv.py` already
    does. Bodies arrive inline, so self-posts and comments owe no fetch — unlike
    the CSV data export, where every row would.
  - Atom has no `after` field, so the cursor is derived from each entry's
    `<id>`, which *is* a fullname. A page contributing no new fullnames ends the
    walk, so a server ignoring the cursor cannot spin the page budget. Reddit
    serves 25 without an explicit `limit`, so the loader always asks for 100.
  - The URL is a credential: `.secrets` only, redacted in every error, and the
    `httpx` logger is quietened around the request because httpx logs the full
    URL — token included — at INFO.
  - **Incremental by default, backfill on request.** The walk stops at the first
    fullname already in the archive, which is correct because the listing is
    ordered by save time. The stop signal is fullnames, not dates: Atom's
    `<updated>` is creation time, so an old post saved today would end a
    date-based walk on the very item that is new. `alexandria reddit --backfill`
    walks everything. The ingest phase still walks fully — it needs bodies for
    anything missing chunks, which are not necessarily recent saves.
  - **Throttled paging** (`reddit_feed_poll_interval_seconds`, default 1.0, plus
    up to `reddit_feed_poll_jitter_seconds` of jitter) between pages only, so an
    incremental sync that ends on page one never sleeps.
- **The Reddit OAuth (PRAW) route is gone.** The feed above is the only loader:
  `load_saved` *is* the Atom loader, and `reddit_feed_url` the only credential.
  Creating the "script" app PRAW needs requires an API-access clearance personal
  accounts do not get, so the path could not be exercised — it was five dead
  settings (`reddit_client_id`, `_client_secret`, `_username`, `_password`,
  `_refresh_token`), the `[reddit]` extra, and a test suite for code that never
  ran. **Migration:** delete those keys from `.env` — `Settings` forbids unknown
  `ALEXANDRIA_*` env keys, so a stale one now fails startup. Stale entries in
  `.secrets` are only warned about.
- **Reddit is no longer hidden behind the experimental-sources toggle** now that
  it ingests end to end; YouTube still is.
- **Backfill is reachable from the UI**, not just `alexandria reddit
  --backfill`: `POST /ingestion/sync/{source}/metadata?backfill=1` threads the
  flag to the handler, and a "Backfill" button sits beside "Sync metadata" for
  sources that have one. Asking for a backfill on a source without an
  incremental sync is a 400 rather than a silent no-op — every other connector
  reads its whole local database each run, so the request can only be a caller
  bug. `BACKFILL_SOURCES` exists on both sides (router and `sources.ts`) and the
  frontend copy names the backend one.

- **Reddit auth was broken by our own client construction.** `_build_client`
  set `reddit.read_only = True`, which in PRAW is not a "never write" flag: the
  setter swaps `_core` to the `ReadOnlyAuthorizer` (application-only
  client-credentials grant), discarding the script/refresh authorization just
  built. Saved items are user-scoped (`/user/<name>/saved`), so `user.me()`
  raised `ReadOnlyException` before any credential was exercised — no login
  could ever work. The line is gone; reads stay reads because the connector
  only calls listing endpoints. Failures from `user.me()` and from the saved
  listing now translate to `RedditConnectorError` naming the actual checks
  (script-type app, developer on the app, client id vs app name, SSO accounts
  have no password), with the original exception chained. Tests now exercise
  `_build_client` against a stubbed `praw` module — every existing test injected
  a ready-made client, which is how this survived.

- **CLIP is now opt-in and off by default** (`ALEXANDRIA_CLIP_ENABLED`, new
  `DESIGN.md` §3.3). It buys one thing the rest of the pipeline cannot —
  matching a query whose words appear nowhere in the image's inferred text — and
  charges a ~600 MB model download, a fourth pass per image, and a second Chroma
  collection for it. Ingestion applies the flag where `ocr_enabled` is applied
  (`image_sync` and the CLI pass `skip_clip`), and `search_images_by_text`
  returns `[]` before loading the model, so `/search`, `/images/search` and
  `alexandria images --search` inherit the gate without their own flag checks.
- **Second image search path: the text inferred from the picture.**
  `search_images_by_inferred_text` queries the shared chunk collection filtered
  to `source=image` — the per-type content extraction, description, and OCR that
  §3.2 already writes — and collapses chunks to the best one per document, so a
  shelf photo is one result rather than one per synopsis. This is what keeps
  images findable with the visual index off. `/images/search` now runs both
  paths (`mode=hybrid|clip|text`, default hybrid), merges them by best
  similarity, and reports `matched_by` (`clip` | `text` | `clip+text`) on each
  hit; `/search` needed no change, since its semantic branch already queries the
  collection those image chunks live in.
- **Ingestion progress counts images by `indexed_at`**, not by the presence of
  a `clip_vector_id` / `text_vector_id`. The old predicate would have reported
  zero embedded images once CLIP went opt-in — and `text_vector_id` has never
  been written at all (image chunks are keyed by `document_id` in `chunks`),
  so it was carrying the count alone.

- **Local-only is now a default, not a constraint** (`DESIGN.md` §1.1). The
  motivation is hardware: the primary machine cannot run models large enough for
  long-document summarisation or reliable vision extraction, and capping every
  capability at what it fits held quality below usefulness. The replacing rules —
  local by default, every outbound path a named default-off setting, no implicit
  escalation between flags, credentials in `.secrets` — are written down, along
  with a table of what actually crosses the wire per category: inference
  providers see **document content**, enrichment lookups see **derived
  identifiers** (library inventory), source connectors see **nothing new**.
  Telemetry and analytics stay prohibited in every configuration.
- `DESIGN.md` §2.1 no longer claims YouTube is the *only* network connector — it
  never was, since Reddit is one too (§3 already said so). The section is now a
  template for the shape a network connector should have.
- New `DESIGN.md` §3.2 documents the retrieval-enrichment design: the systematic
  gaps in what each source contributes to the index (Firefox/Reddit embed body
  text with no title and can yield zero chunks; Calibre full text has no
  document-level statement; image descriptions describe *appearance*), and the
  three mechanisms that close them in ascending cost — deterministic title +
  `card_summary`, per-type local VLM prompts, then external lookup or generated
  summaries. Includes the per-doctype table, the shared resolution ladder
  (checksum-validated ISBN → Open Library round-tripped → web search, always
  queried from `(title, authors)` rather than a model-authored string), and the
  guardrails (`pass=` tagging, never overwrite `description` / `card_summary`,
  CLIP untouched, cache generated text, 2–4 sentences for MiniLM).
- **Per-type image prompts implemented** (§3.2 mechanism 2). The single generic
  "describe this image" prompt is replaced by one prompt per category:
  a *transcript* for `slide` / `notes` / `whiteboard`, a *content summary* for
  `poster` (explicitly told not to resolve a publication), and structured
  `{"books": [{title, authors, isbn}]}` extraction for all three book labels
  (`book_cover`, `multiple_book_covers`, `bookshelf`), differing only by a hint
  line about what is legible — spines yield partial entries, and the prompt
  forbids inventing a title, since these feed identifier lookups later.
  `extract_image_content` selects the prompt from the label the admission gate
  already resolved, so the better prompt costs **no extra model call**; with the
  gate off (or `--skip-gate`) it falls back to classify-then-prompt. Consequence
  worth knowing: when the gate runs, `images.image_type` and the
  `TagOrigin.INFERRED` overlay tag now come from the gate model's label rather
  than the main `vision_model`'s. Each content prompt also returns its own
  artifact-level `description`, so `images.description` / `card_summary` stay a
  truthful caption of the photo while the transcript/summary/book lines are what
  `image_search_text` (still the single assembly point) puts in the index.
  Extracted book fields are returned from `ingest_image` and cached in the new
  `images.books_json` column for a later lookup — no lookup, network call, or
  flag is added here.
- **Book-synopsis cascade wired into the image pipeline** (§3.2 mechanism 3,
  default off). Each book the cover/shelf prompt extracted is resolved through
  `pka/ingestion/openlibrary.py` — checksum-validated ISBN first, then a
  title+author search whose canonical result must round-trip against what was
  extracted — and the description is attached as **its own chunk per book**,
  tagged `pass="external_synopsis"` with the resolved identity. One chunk per
  book rather than one blob, so a shelf photo can match a query for any single
  title on it instead of diluting ten synopses into one vector.
  `images.description` and `card_summary` are untouched, so the browse card
  stays a truthful caption of the photograph; CLIP vectors are untouched too.
  A lookup
  failure is logged and skipped — enrichment never costs an image its ordinary
  ingestion. The gate is `lookup_book`'s single `external_lookup_enabled` check,
  so with the flag off the loop resolves nothing and issues no request.
- **Third rung of the book ladder** (`pka/ingestion/book_search.py`, default
  off). Reached only when Open Library does not hold a book. Implemented as a
  *second catalogue* rather than the general web search originally planned:
  the job is turning `(title, authors)` into a description, and a search engine
  answers that with retailer pages needing scraping where a catalogue answers
  directly with canonical fields the same round-trip check can verify. Google
  Books is the default — documented, free, and **keyless**, so a default-off
  feature is switch-on-and-try; a key only raises the quota. The rung sits behind
  a `(title, authors) -> BookSynopsis | None` provider registry, so Brave or
  Tavily drops in as one callable and one entry with no cascade change. Gated on
  `cover_search_active`, not `cover_search_fallback`, so the fallback flag
  genuinely cannot be what first opens a network path.
- `search_provider` is now a comma-separated **chain** tried in order, so a
  weaker backend can run *after* a stronger one rather than replacing it
  (`google_books,brave`). A **Brave** web-search provider ships alongside Google
  Books: it needs a key, returns a search snippet rather than a curated synopsis,
  and verifies one-directionally (a search engine has no canonical title field,
  so the extracted title must appear in the page title and an extracted author in
  the title or snippet) — looser by necessity, hence last. Results are tagged
  `resolved_by="brave"` so they stay auditable and separately purgeable. Listing a
  backend with no key configured skips that rung instead of erroring.
- **Generated summaries wired in** (§3.2 mechanism 3, default off).
  `pka/ingestion/summarize.py` does bounded local map-reduce: input already
  within the sentence cap (most bookmarks) short-circuits with no model call,
  and longer input is chunked, mapped and reduced under hard caps on both
  chunks-per-pass and reduce depth, so a bulk ingest cannot run away. Attached
  via `core.attach_summary_chunk` as its own `pass="summary"` chunk and cached in
  the new `documents.generated_summary` column, so a purge-and-reingest replays
  without paying for inference twice. Wired into the Firefox/Reddit fetched-text
  path (which is also where long articles live) and the Calibre full-text pass.
  Gated per source: `bookmark_summary_enabled` for bookmarks and posts,
  `book_summary_enabled` for Calibre — separate because a book is map-reduced
  over its whole text where a bookmark is usually one call, and enabling the
  cheap case must not enable the expensive one.
- **Calibre joins the book ladder.** The metadata pass attaches an external
  synopsis chunk, but *only when Calibre holds no description of its own* — pass
  1 already embeds the publisher blurb, so looking one up would be redundant text
  and a wasted request. `CalibreBook.isbn` feeds the ISBN rung directly, so books
  with an identifier resolve exactly rather than by title match.
- `trim_to_sentences` moved from `openlibrary.py` into `chunker.py`, where both
  callers can reach it — a summariser importing a book-lookup module for generic
  sentence handling was the wrong dependency edge.
- `DESIGN.md` §3.1 corrected: it still claimed the main pipeline re-classifies
  with `vision_model` after the gate, which the per-type prompt work made untrue.
- `tests/conftest.py` pins the three enrichment flags off suite-wide, so a
  developer's real `ALEXANDRIA_EXTERNAL_LOOKUP_ENABLED=1` cannot send the tests
  to openlibrary.org.
- Settings documented in `.env.example`: `bookmark_summary_enabled`,
  `external_lookup_enabled`, `cover_search_fallback` (requires the former),
  `search_provider` + `SECRET_ALEXANDRIA_SEARCH_API_KEY`. All default off. The
  per-type VLM prompts are on by default and purely local — no flag.

## Unreleased — Ollama Cloud provider

- New **`ollama_cloud`** backend for the `chat`, `vision`, and
  `image_gate_vision` capabilities, alongside the existing `ollama`,
  `openrouter`, and `ovh`. It targets hosted `ollama.com`, which speaks the same
  native `/api/chat` as the local daemon, so `pka/providers/ollama.py` covers
  both: the two provider classes gained `base_url` / `api_key` / `model` /
  `label` / `remote` constructor args and send `Authorization: Bearer …` when a
  key is set. Zero-arg construction is unchanged, so local behaviour is
  byte-identical.
- `remote=True` disables the local-only conveniences that would misfire against
  a hosted endpoint: no `/api/tags` auto-detection, and no fallback to
  `chat_model` / `vision_model`. A missing key or model is returned as an error
  (chat) or raised (vision) instead of silently sending a local model name to
  the cloud. As with the OpenAI-compatible backends, a model baked in at
  construction wins over the per-call override — which is what lets the image
  gate pin `image_gate_vision_model`.
- Config: `ollama_cloud_base_url` (default `https://ollama.com`),
  `ollama_cloud_api_key`, `ollama_cloud_chat_model`, `ollama_cloud_vision_model`.
  The key is a credential and belongs with the others as
  `SECRET_ALEXANDRIA_OLLAMA_CLOUD_API_KEY`.
- The pre-existing route — `ollama signin` plus a `:cloud`-tagged model in
  `chat_model`, proxied by the local daemon — still works and needs none of
  these settings; `.env.example` and `README.md` now document both.

## Unreleased — credentials move to `.secrets`

- **Credentials now live in a `.secrets` file**, separate from `.env`. Same
  `KEY=value` shape, but each key carries a `SECRET_` prefix on top of the usual
  one (`SECRET_ALEXANDRIA_OPENROUTER_API_KEY`). A new
  `SecretsFileSettingsSource` in `pka/config.py` strips the prefix and feeds the
  rest through the normal settings machinery, so the same `Settings` fields are
  populated with **no call-site changes**.
- **Precedence:** process environment > `.secrets` > `.env` > code defaults. A
  real env var still wins; a secret overrides anything left in `.env`.
- Keys lacking the `SECRET_` prefix, and keys matching no setting, are ignored
  with a warning — the file can't be used to set arbitrary config behind
  `.env`'s back. `ALEXANDRIA_SECRETS_FILE` relocates it; setting that empty
  disables the source, which `conftest.py` now does suite-wide so tests can
  never pick up a developer's real credentials.
- `.env.example` documents the split, `.secrets.example` is the new template,
  and `.secrets` is git-ignored.
- **Agent guardrail:** `Read` deny rules for `.secrets` in
  `.claude/settings.json`. Per the Claude Code permissions docs these cover the
  built-in file tools *and* the Bash file commands it recognises (`cat`, `head`,
  `tail`, `sed`) — verified: `head -c 1 .secrets` is refused.
  They do **not** cover arbitrary subprocesses that open the file themselves
  (`python -c "..."`). OS-level enforcement for those needs the Bash sandbox
  (`sandbox.filesystem.denyRead` / `sandbox.credentials.files`), which runs on
  macOS/Linux/WSL2 only — not native Windows. Worth enabling if this repo's
  Claude Code sessions move to WSL2.
  A regex `PreToolUse` hook was tried and removed: extending it to catch
  programmatic reads means denylisting identifiers, which any indirection
  defeats. The real mitigation is that credentials are no longer in `.env`.

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
- **Classifier labels are normalised before the enum check.** Vision models
  answer with the label as prose — moondream returns `"book cover"`, not
  `"book_cover"` — and the strict `not in _VALID_TYPES` test folded every such
  answer to `unknown`. At the gate that rejected *correctly classified* images
  and cached the rejection permanently: a 10-image book-cover library was
  rejected 10/10 while the model was identifying every one of them. Case,
  spaces, and hyphens are now folded to the underscore form (`_normalize_type`,
  applied on both the JSON and salvage paths).
- **Progress counters are scoped to admissible images.** `count_pending_metadata`
  and `source_corpus_size` counted every scanned file, including cached
  rejections that both passes skip — so a metadata sync pinned a total it could
  never reach (stuck at e.g. `8 / 10`), and because a gate rejection *deletes*
  the registered rows, the processed count then walked backwards (`8 → 2 → 0`)
  against that fixed total. Both probes (and the ingest phase plan in
  `sync_images_ingest`) now count through `admitted_images`, matching what the
  passes actually persist.
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
