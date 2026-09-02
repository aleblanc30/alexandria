# TODO

High-priority work: one brief line per item.

Nice-to-haves live in `BACKLOG.md` instead. Neither file requires a detailed plan —
the split is priority, not how well an idea is worked out. Move an entry between the
two when its priority changes; do not duplicate it in both.

## Maintainability & performance

Quick wins from `MAINTAINABILITY_PERFORMANCE_AUDIT.md` §6 (items 1-2, 4-5 of the prioritised plan):

- [x] **P-1: index unindexed foreign keys** — added `sa.Index(...)` to `pka/db/schema.py` for `source_tags(document_id, tag_string)`, `source_collections.document_id`, `cluster_assignments(run_id, document_id)`, `images.document_id`, `fetch_log.document_id`, `reading_list_items(list_id, document_id)`, `chunks(document_id, chunk_index)`, plus matching `CREATE INDEX IF NOT EXISTS` migrations in `init_db` and new `_MIGRATED_INDEXES` cases in `tests/test_schema_migration.py`. Verified with `EXPLAIN QUERY PLAN` that the source-tag `EXISTS` and cluster-assignment filters now `SEARCH ... USING COVERING INDEX` instead of scanning. `docs/persisted-fields.md` does not track indexes, so no change needed there.
- [ ] **P-2: lazy sklearn/chroma imports at API startup** — `pka.api.routers.clusters` → `pka.clustering.engine` → `sklearn.decomposition` costs 3.0s of the 4.7s cold `import pka.api.main`; move sklearn imports inside the functions that use them and make `routers/clusters.py` import the engine lazily like `runs.py` already does.
- [x] **M-7: add mypy config, baseline-ratcheted** — added `[tool.mypy]` to `pyproject.toml` (pydantic plugin, `warn_unused_ignores`) plus a `[[tool.mypy.overrides]]` freezing the 21 modules that had pre-existing errors with `ignore_errors = true`, so `mypy pka` now exits 0 and the gate is "no new errors" rather than "fix 89 errors first". Adding the pydantic plugin turned out to fix `routers/runs.py`'s 7 `call-arg` false positives outright, so that module needed no override. Added `mypy pka` to the CLAUDE.md verify table (not the check-script/CI part — that's M-12, below). Also fixed the `ruff format` drift in `tests/test_connector_reddit.py` and the two deprecation warnings from M-11 (`routers/trends.py:62` `utcfromtimestamp` → `fromtimestamp(ts, UTC)`, `config.py`'s inner `class Config` → `model_config = SettingsConfigDict(...)`).
- [ ] **M-12: add an automated check script** — no CI/pre-commit/Makefile exists; add `scripts/check.ps1` + `scripts/check.sh` running `ruff check`, `ruff format --check`, `mypy pka`, `pytest --cov`, and the two `npm` commands, then wire it into CI or a pre-push hook.
- [ ] **M-13: hygiene batch** — delete `pka/pipeline.py` (deprecated shim, zero importers) and its `coverage.omit` line; remove vulture-flagged unused `all_runs` (`cli/purge_cluster_runs.py:25`) and `base_netloc` (`connectors/reddit.py:447`); hoist the 8420/8421 port split into one shared constant in `pka/constants.py` read by `cli/dev.py`.

## Ingestion & deduplication

- [ ] **Deduplication of tags** — merge or collapse duplicate tag names (case, spacing, synonyms) so the tag index stays clean.
- [ ] **Deduplication of items** — detect and merge duplicate documents/items across sources (same URL, DOI, arXiv ID, etc.) instead of storing multiple records. `documents.doi` / `arxiv_id` / `isbn` are now joinable (indexed, canonical form) — see `archive/DOCUMENT_METADATA_PLAN.md`.
- [x] **Persist structured document metadata** — every runner flattens authors/DOI/year into the embed blob, so the chunk text is the only copy; add `doi` / `arxiv_id` / `isbn` (all indexed), `year` and `authors_json` to `documents`, derive the DOI of a preprint from its arXiv ID, and split Zotero's `url_or_path` into `zotero_url` / `zotero_path`; plan in `archive/DOCUMENT_METADATA_PLAN.md`.
- [x] **Collapse the `documents` write-path signature** — `insert_document_if_new` and `upsert_document` now take a single `DocumentWrite` dataclass, backed by one Core `sqlite_insert(...).on_conflict_do_update(...)` writer with a default-COALESCE update policy; a new column is one dataclass field, not six hand-parallel SQL lists. Plan in `archive/DOCUMENT_WRITE_PATH_PLAN.md`.
- [x] **Domain frequency report** — list all domain names from ingested items, sorted by frequency, to prioritize which domains deserve special fetch/handlers next.
- [ ] **Ingest Zotero PDF attachments** — `item.pdf_path` is recorded and never read, so Zotero indexes title + abstract only (DESIGN.md §3.2); needs a phase-2 pass mirroring `ingest_calibre_fulltext` over `extract_book_report`, offset by `existing_chunk_count()` — no new extraction machinery.
- [ ] **Exempt preprint PDFs from the page cap** — `fetch_pdf_max_pages` caps every PDF route at 3 pages, so arXiv/bioRxiv now index only title + abstract + 3 pages.
- [x] **Make `_DomainRateLimiter` actually rate-limit** — `wait` now claims a slot under the lock instead of deriving a delay from a shared last-send time, so concurrent waiters on one domain are spaced rather than released together.
- [ ] **Domain-aware fetch dispatch** — the fetch pool picks a URL off a flat queue and only then waits on the per-domain limiter, so a run of same-host URLs parks every worker on that host while other domains sit ready; plan in `FETCH_DISPATCH_PLAN.md`.

## Search / vectors

- [ ] **Evaluate a different text embedding model** — Chroma's `DefaultEmbeddingFunction` (`all-MiniLM-L6-v2`) is used as-is in `pka/storage/vector_store.py`; benchmark retrieval quality against something like `bge-small-en-v1.5` or `gte-small` before committing, since swapping models forces a full reindex via `rebuild_from_chunks` (Chroma collections are dimension-locked).
- [x] **`upsert_chunks` doesn't batch under Chroma's max batch size** — `pka/storage/vector_store.py` passed the whole chunk list straight to `collection.upsert()`; a large document (seen: 14584 chunks from a Firefox fetch) blew past Chroma's `max_batch_size` (5461) and raised `chromadb.errors.InternalError`, so the whole doc's embedding failed (`embed_fetched_text` in `runners/firefox.py:109`) and was left with zero chunks. Now loops over `_UPSERT_BATCH_SIZE` (5000, fixed constant — same pattern as `_GET_PAGE_SIZE`/`_DELETE_BATCH_SIZE`) slices of `ids`/`texts`/`metadatas` before upserting.

## Clustering

- [x] **`adaptive_cluster_params` manufactures the clustering noise** — HDBSCAN infers its own cluster count, but `pka/clustering/engine.py:289` back-solved `min_cluster_size` from a `target_clusters` that `min(12, …)` pinned at 12 for any corpus over 144 docs, so `min_cluster_size` scaled with the archive instead of staying fixed (744, with `min_samples=372`, at 17.9k docs) — the actual cause of run #4's ~83% noise, not an HDBSCAN property. Now derives `min_cluster_size` from `sqrt(n_docs)` capped at 50 (`_ADAPTIVE_MAX_CLUSTER_SIZE`), so it stays in a browsable absolute range regardless of corpus size.
- [ ] **Two-level agglomerative clustering algorithm** — `pka/clustering/engine.py` only ever runs HDBSCAN (over PCA or legacy UMAP space); add agglomerative (ward) clustering as a third `cluster_space` mode, reusing the existing L1/L2 hierarchy machinery (`_write_hierarchical_clusters`, `_run_level2_pass_core`'s callback seam) rather than rebuilding it. Partitions every document rather than leaving most as noise, and picks its cluster count by cutting a prebuilt dendrogram. Plan in `AGGLOMERATIVE_CLUSTERING.md`.
- [ ] **Suggested merges in cluster diagnostics cannot be performed** There is no backend or UI surface to actually perform the merge
- [ ] **OpenRouter free-tier model returns 200 OK with no `choices` key, breaking LLM cluster labelling** — `OpenAICompatProvider.chat_json` (`pka/providers/openai_compat.py:69`) does `body["choices"][0]["message"]["content"]` unguarded; seen failing for `nvidia/nemotron-3-ultra-550b-a55b:free` (`openrouter chat failed (model=...): 'choices'`), silently falling back to tf-idf labelling every time (`pka/clustering/engine.py` "LLM labelling failed: 'choices' — using fallback"). Log `body` (or `body.get("error")`) on this failure so it's diagnosable instead of just the KeyError text, and consider whether this free model is reliable enough to keep as a default.
- [x] **Clustering diagnostics are too slow** — `/runs/{id}/diagnostics` pulls every chunk vector of the whole archive out of Chroma twice per request (`compute_drift` and `compute_merge_suggestions` each call `_get_cluster_centroids`), recomputing what the run already held in memory; persist per-cluster centroids at run end and serve merges/drift from them. Plan in `archive/CLUSTER_DIAGNOSTICS.md`.
- [ ] **Investigate the recurring 15430-document "unassigned" pool** — `assign_new_docs` (`pka/clustering/lifecycle.py:290`, called automatically by `_assign_new_documents` in `api/routers/ingestion.py` after every completed ingest, by design — it's a deliberate nearest-centroid fallback, not a full HDBSCAN re-run) logs the same ~15430-document count getting reassigned on repeated ingests. Confirm whether these are HDBSCAN-noise-labelled docs that never land a real level-1 cluster and just get re-filed to the nearest centroid every cycle, vs. a case where assignments aren't actually persisting. Related to the noise-ratio fix already landed above (`adaptive_cluster_params`).

## Source connectors

- [x] **arXiv ingester** — Firefox fetch handler for arxiv.org (`pka/ingestion/arxiv.py`): export.arxiv.org API metadata + PDF; title and abstract on browse cards.
- [x] **bioRxiv ingester** — Firefox fetch handler for biorxiv.org (`pka/ingestion/biorxiv.py`): api.biorxiv.org DOI lookup + PDF; title and abstract on browse cards.
- [x] **Youtube ingester** — Firefox fetch handler for youtube pages saved as bookmarks (`pka/ingestion/youtube_bookmark.py`); plan in `archive/FIREFOX_INGESTERS_PLAN.md`.
- [x] **reddit ingester** — Firefox fetch handler for reddit pages saved as bookmarks (`pka/ingestion/reddit_bookmark.py`), with a URL-derived title/subreddit fallback when the `.json` listing is blocked; plan in `archive/FIREFOX_INGESTERS_PLAN.md`.
- [x] **amazon ingester** — `is_amazon_host` already matched any TLD (`.fr`, `.co.uk`, `.in`, ...); locked in with a regression test, see `archive/FIREFOX_INGESTERS_PLAN.md` §0.
- [x] **pubmed ingester** — Firefox fetch handler for pubmed pages saved as bookmarks (`pka/ingestion/pubmed.py`); plan in `archive/FIREFOX_INGESTERS_PLAN.md`.
- [x] **top domains and top rejected domains** — `GET /ingestion/domains` and a two-table panel on `/ingestion` rank domains by document count and by unfetchable count; plan in `archive/DOMAIN_TOP_LISTS_PLAN.md`.
- [x] **Search-URL cards** — a bookmarked search-results page (`google.com/search?q=`, `duckduckgo.com/?q=`, `youtube.com/results?search_query=`) is scraped as if it were a document; decode the query from the URL into a title + card summary with **no HTTP request at all**. Plan in `archive/SEARCH_URL_CARDS.md`.
- [ ] **nature.com fetch handler** — needs a dedicated Firefox fetch handler (paywall/anti-bot page currently scraped as-is); article path already carries the `10.1038` DOI suffix. Plan in `PUBLISHER_FETCH_HANDLERS.md` §6.
- [ ] **doi.org fetch handler** — DOI redirect target isn't resolved/handled, so the landing page is scraped as-is; resolve by content negotiation instead of following the redirect. Plan in `PUBLISHER_FETCH_HANDLERS.md` §5.
- [ ] **sciencedirect.com fetch handler** — top unfetchable domain; needs a dedicated Firefox fetch handler (paywall/anti-bot page currently scraped as-is). PII → DOI via Crossref `alternative-id`, which misses on older deposits. Plan in `PUBLISHER_FETCH_HANDLERS.md` §8.2.
- [ ] **link.springer.com fetch handler** — needs a dedicated Firefox fetch handler alongside the other publisher domains above; DOI is already in the path, but Crossref carries no abstract for `10.1007` so the Semantic Scholar rung is mandatory. Plan in `PUBLISHER_FETCH_HANDLERS.md` §8.1.
- [ ] **mitpress.mit.edu fetch handler** — top unfetchable domain; needs a dedicated Firefox fetch handler. **Not a DOI handler** — it is a bookstore with the ISBN in the URL, so it reuses the Open Library ladder. Plan in `PUBLISHER_FETCH_HANDLERS.md` §9.
- [ ] **journals.aps.org fetch handler** — top unfetchable domain (hard 403); needs a dedicated Firefox fetch handler. DOI in the path; `link.aps.org` is the same handler. Plan in `PUBLISHER_FETCH_HANDLERS.md` §7.
- [ ] **researchgate.net fetch handler** — top unfetchable domain; hard-blocked with no public API, so the handler builds a card from the URL slug with **no request at all** (the `search_url.py` pattern). Plan in `PUBLISHER_FETCH_HANDLERS.md` §10.


## Active learning

- [ ] **Show performance stats for active learning labels** — surface `train_stats` in the tag-training UI (accuracy, precision/recall on a hold-out slice of user labels, positive/negative counts, skipped embeddings) so the user can judge model quality before accepting.
- [ ] **Improve tagging interface to allow negative seed** — extend seed affordances (source tag and browse multi-select) so the user can mark documents as negative examples (`label=0`, `source=seed`) at session start, not only positives via the Yes/No queue or auto bootstrap.

## UI

- [ ] **Make top unfetchable domains list collapsible** — `DomainTopLists.vue` displays the "Top unfetchable domains" section with a full table; add a collapse/expand toggle so it doesn't take up space when closed.
- [x] **Settings panel (read-only environment report)** — nothing in the UI reflects any of `config.py`'s settings and there was no health endpoint anywhere, so a misrouted provider or an unreachable Ollama was invisible (the direct diagnostic for *Summarization calls fail silently* above); `GET /settings` (`pka/api/settings_view.py` + `routers/settings.py`) plus a `/settings` view show resolved provider/model per capability, credential presence (never the value), the §1.1 outbound flags and every non-default value, grouped and with a per-capability reachability check. Writes are a separate, later slice — see `BACKLOG.md` and `SETTINGS_PANEL.md` §6.
- [ ] **Delete tags in the UI** — allow removing tags from the frontend (with appropriate API support and confirmation).
- [x] **Cluster run parameter dialog** — `+ New run` on `/runs` opens `ClusterRunDialog.vue`, exposing the `run_clustering()` knobs the CLI already had (method, min cluster size / samples / neighbours, min dist, PCA or UMAP dims, labelling mode) via a `TriggerRunRequest` JSON body. Plan in `archive/CLUSTER_RUN_DIALOG.md`.
- [x] **Cluster run deletion interface** — `DELETE /runs/{run_id}` (optional `?force=true` for an accepted run) wraps the already-existing `purge_cluster_run` from `pka/cli/purge_cluster_runs.py`; `/runs` gets a per-row Delete button (window.confirm, force-confirm wording when deleting the active run).
- [x] **Cluster run stop button is not functional** — `_label_clusters`'s `ThreadPoolExecutor` blocked cancellation behind `shutdown(wait=True)` in `__exit__`; it now shuts down with `cancel_futures=True` on `ClusterRunCancelled` instead of waiting for the whole in-flight labelling batch.

## CLI & assistant

- [x] **Build a CLI** — shipped as `pka/cli/` and the `alexandria` console script; `scripts/*.py` remain as thin shims.
- [ ] **Add chat/agent capability** — local-first conversational interface over the archive: retrieve relevant documents via semantic search, answer questions with Ollama, and cite source items (UI panel and/or CLI subcommand).

## MCP

- [ ] **MCP server for document search** — architect and ship an MCP server that lets a client search for documents in Alexandria; plan in `MCP_PLAN.md` (Zotero + Firefox first, read-only, HTTP client of the local API).

## Installation

- [x] **Start menu shortcuts** — `INSTALL.md` §8 documents Start / Console / Stop shortcuts for the standalone install, backed by shipped `scripts/console.bat` (follows `server.log`) and `scripts/stop-server.bat`.

## Ingestion

- [ ] Summarization calls fail silently.
- [ ] **Selective purge & pipeline re-triggers** — buttons to purge specific subsets (summaries, vectors, image text, machine tags, fetched text) and retrigger the matching pipeline step, so swapping an embedding/summarisation backend does not require nuking a whole source; includes provenance stamping so a purge can target "made by the old model". Plan in `PURGE_AND_PROVENANCE_PLAN.md`.
- [x] **Batch the purge path's IN lists** — `_purge_documents` / `_purge_images` (`pka/cli/purge_source.py`), `purge_vectors` (`pka/storage/vector_store.py`) and `delete_clip_vectors` (`pka/ingestion/image_pipeline.py`) now batch on 5000 the way the clustering read path does, so a source over SQLite's 32766-variable cap no longer fails with `too many SQL variables`. The two Chroma `delete(ids=...)` calls batch per batch, so one bad batch does not strand the rest.
- [ ] **Use zotero collection names as tags** — plan in `COLLECTION_TAGS.md`, which extends it to Firefox bookmark folders as the second (arguably higher-value) source.
- [ ] **Extend the summarization call to include tag inference**
- [ ] **Train a classifier for the image gate instead of the vlm** — replace the per-image `moondream` call in `pka/ingestion/image_gate.py` with a frozen CLIP backbone (`providers/clip.py`, already wired) plus a trainable linear head, trained from `data/image_gate_training/<label>/` folders via a new `alexandria image-gate-train` CLI. Must be multi-class over `_VALID_TYPES`, not binary — the gate's label feeds `images.image_type`, the inferred tag, and the content prompt. Plan in `IMAGE_GATE_CLASSIFIER.md`.
- [ ] **Invetigate whether backfill for reddit is actually useful**
- [ ] **Add ingestion from stored .jsonl for reddit** This ingestion route at the start of the sync would prevent excessive polling from reddit.
- [x] **Source probes redundantly reload the same connector data** — `count_pending_metadata`/`source_corpus_size` and each sync job's own connector call independently re-read Firefox/Calibre/images, logging "Found N / Loaded N" 2-3x per sync. Added `load_firefox_bookmarks` / `load_calibre_books` / `load_scanned_images` to `pka/ingestion/pending_metadata.py`, sharing the existing TTL probe cache (kind `"raw"`); `firefox_sync.py`, `calibre_sync.py`, and `image_sync.py` now call these instead of the connector directly, so pending + corpus + the sync job's own read collapse to one connector read per TTL window.
