# Maintainability & performance audit, September 2026

Audit of Alexandria v0.0.8 at commit `e732b91` (trunk, 2026-09-02). Static
analysis plus the existing test suites; **no profiling against a real archive**
was done (CLAUDE.md forbids running real ingestion), so every performance
finding below argues from code shape rather than from a measurement. Where a
number would change the priority, the finding says what to measure.

This is a proposal document like the rest of `planning/`: nothing here is
authoritative about current behaviour. Items are numbered `M-n`
(maintainability) and `P-n` (performance) so `TODO.md` / `BACKLOG.md` lines can
point back at them. Findings that duplicate an existing planning item say so
rather than re-proposing it.

## 1. Method

The review followed the usual audit axes for a Python/TypeScript service
(complexity, module size and cohesion, coupling, duplication, dead code, test
health, typing, exception hygiene, tooling/process) and the FastAPI-specific
performance checklist (blocking work on the event loop, N+1 and unindexed
queries, over-fetching, startup cost, background-work lifecycle). The published
guidance consulted is listed at the end.

Tools run (all from the repo's `.venv`, radon/vulture/pylint installed into a
scratch directory, nothing added to the project):

| Check | Result |
|---|---|
| `ruff check pka tests scripts` (project rules) | clean |
| `ruff format --check` | 1 file drifted: `tests/test_connector_reddit.py` |
| `ruff check pka --select C901,PERF,SIM,PLR09xx,ARG,RET,TRY,BLE,S110,S112` | 219 findings (TRY003 60, BLE001 49, PLR0913 32, C901 14, PLR0911 9, PLR0912 8, PLR0915 6, S110/S112 5) |
| radon cyclomatic complexity | 47 functions at CC ≥ 11; worst: `search` 73, `init_db` 44, `reddit.load_saved` 26, `cli/dev.main` 25, `parse_arxiv_atom` 23 |
| radon maintainability index | `clustering/engine.py` **5.6 (C)**, `db/queries.py` 14.7 (B), `tag_training/lifecycle.py` 16.5 (B); every other module A |
| mypy (`--ignore-missing-imports`, no project config exists) | 89 errors in 22 files |
| vulture ≥ 70 % confidence | 6 items (2 real, 4 pydantic-validator false positives) |
| pylint `duplicate-code`, ≥ 10 lines | 6 duplicated blocks, all in the fetch-handler family |
| `pytest --durations=25` (Windows) | **1418 passed, 5 skipped, 261 s** |
| `coverage report` on the checked-in `.coverage` (2026-09-01) | 84 % total, below the 85 % gate |
| `python -X importtime -c "import pka.api.main"` | **4.7 s** cold import |
| `npm run build` / `npm run test` | build OK; 46 tests in 7 files pass |

Size, for scale: backend 23.9 k physical / 12.1 k logical lines across 138
modules; tests 20.4 k lines in 66 files (≈1:1 with the code); frontend 5.7 k
lines of TS/Vue.

## 2. Headline

The codebase is in good shape for a single-maintainer project of this size:
ruff is clean and pinned, the test suite is large and fully mocked, N+1 has been
designed out of the hot API paths (`documents_out_batch`, `_browse_tag_maps`,
batched `IN` under SQLite's variable ceiling), SQLite runs in WAL with
`synchronous=NORMAL`, document embeddings are cached in SQLite so clustering
does not re-read Chroma, and the design/derived docs are unusually thorough.
Sync `def` route handlers are the correct choice for a sync SQLAlchemy stack,
and the few `async def` handlers hand blocking work to `run_in_threadpool`.

The problems are concentrated, which makes them tractable:

1. **Three modules hold most of the complexity**: `clustering/engine.py`,
   `db/queries.py`, and the `search` route. They are also among the most
   churned files. (M-1 … M-3)
2. **Several foreign-key columns have no index**, and the browse/tag filters
   are correlated `EXISTS` sub-queries over exactly those columns. (P-1)
3. **The API imports scikit-learn at startup** through the clusters router:
   3 of the 4.7 s cold start. (P-2)
4. **Verification is entirely manual**: no CI, no pre-commit hook, mypy
   installed but unconfigured and never run, coverage currently under the gate,
   one file out of format. (M-12, M-7)

## 3. Maintainability findings

Ordered by expected payoff. "Effort" is a rough S/M/L.

### M-1: `pka/clustering/engine.py` needs splitting (L)

Evidence: 1,620 lines, MI 5.6 (the only C-grade module), 11 of the 32
too-many-arguments hits, `run_clustering` CC 20 with 12 keyword parameters,
`_run_legacy_pipeline` / `_run_pca_pipeline` returning **9- and 10-tuples**
that `run_clustering` destructures positionally (`engine.py:1508-1550`),
6 function-level imports, coverage 62 %. It is also the module that owns the
only non-trivial threading in the code base (label pool, relabel thread).

The file already has the seams drawn: the `# ── Step N` banners at lines 84,
190, 209, 287, 373, 842, 1465. Recommendation:

- Split along those banners into `clustering/embeddings.py` (step 1),
  `reduce.py` (PCA/UMAP), `hdbscan_step.py`, `labelling.py` (step 5, ~470
  lines on its own), `persist.py` (step 6), keeping `engine.py` as the
  orchestrator plus `run_clustering`.
- Replace the tuple returns with one `@dataclass PipelineOutput` (labels,
  reduced_2d, label/desc maps, l2 batches, counts, params). `ClusterRunResult`
  and `L2ClusterBatch` show the pattern is already accepted here.
- Group the 12 `run_clustering` knobs into a `ClusterParams` dataclass that
  `TriggerRunRequest` maps onto once; the API schema and the CLI then stop
  hand-mirroring the same defaults.

### M-2: `pka/db/queries.py` mixes engine lifecycle, migrations, and every query (M)

Evidence: 1,233 lines, 46 top-level definitions, MI 14.7, second-highest churn
(34 commits). `init_db` (CC 28, 69 statements) is 45 hand-written
`if "<col>" not in cols: ALTER TABLE …` / `CREATE INDEX IF NOT EXISTS` steps,
each re-executed on every start. It is idempotent, which CLAUDE.md requires,
but every new column is another branch in one function.

Recommendation:

- Move `get_engine` + `init_db` to `pka/db/engine.py` / `pka/db/migrate.py`.
  Express migrations as an ordered list of `(name, fn)` and record the applied
  ones in the existing `meta` table (or `PRAGMA user_version`). Still
  idempotent; each step becomes three lines and testable in isolation
  (`tests/test_schema_migration.py` already exists to host that).
- Split the query helpers by aggregate: `documents.py` (`DocumentWrite`,
  upsert), `chunks.py`, `tags.py` (`list_tags`, overlay/source tag helpers),
  `browse.py` (`_apply_document_browse_filters`, `list_documents`,
  `filter_document_ids`), `clusters.py`. `pka/db/queries.py` can stay as a
  re-export shim for one release, the way `pka/pipeline.py` did.

### M-3: the `search` route is one 160-line function (M)

Evidence: `pka/api/routers/search.py:29`, radon CC 73 (F), ruff C901 28,
27 branches, 75 statements; 13 commits. It interleaves five concerns: semantic
query, fulltext fallback/merge, CLIP merge, browse-filter intersection,
cluster/date/status filtering, then pagination. The docstring at the top of the
file explains the N+1 avoidance, which is good, but every new filter lands in
the same function.

Recommendation: extract `_semantic_hits(req)`, `_fulltext_hits(con, req,
existing)`, `_merge_clip(results, req)`, `_apply_row_filters(con, results,
req, run_id)` as pure functions over `list[tuple[int, float | None]]`, unit
tested directly. `TestSearch` in `tests/test_api.py` (≈270 lines) would then
shrink to endpoint-shape checks. See P-5 for the query changes worth making at
the same time.

### M-4: fetch dispatch is already planned; this audit only adds evidence (none)

`pka/ingestion/fetcher.py::_fetch_one_impl` is CC 26 with **19 return
statements**, `fetcher.py` carries 13 function-level imports, and pylint's
duplicate-code report lands entirely in this family: `arxiv.py:177-210` ≡
`biorxiv.py:139-172`, `arxiv.py:241-260` ≡ `biorxiv.py:209-228`,
`biorxiv.py:107-127` ≡ `reddit_bookmark.py:161-181`, `pubmed.py:131-146` ≡
`reddit_bookmark.py:161-176`, `runners/firefox.py:125-139` ≡
`runners/reddit.py:184-198`. `planning/FETCH_DISPATCH_PLAN.md` and
`planning/archive/PUBLISHER_FETCH_HANDLERS.md` already proposed the handler registry
that fixes this; the duplicated blocks above are the concrete lines a shared
`fetch_base` template should absorb. No new item.

### M-5: exception hygiene: 49 blind `except Exception`, 4 silent `pass` (S)

Evidence (`ruff --select BLE001,S110,S112`): `connectors/reddit.py` 7,
`storage/vector_store.py` 6, `image_pipeline.py` 4, `book_extractor.py` 4,
`fetch_base.py` 3, plus `try/except/pass` at `fetch_base.py:110,125,136`
(the three-rung HTML text extractor swallows *every* failure including
`ImportError`, so a missing `readability` package is indistinguishable from an
empty page) and `connectors/images.py:61`.

Many of these are deliberate "never let one document kill the sync" guards and
should stay. Recommendation: enable `BLE001` and `S110` in `pyproject.toml`
with `# noqa: BLE001` on the intentional ones (the comment records the
intent), and make every remaining catch either narrow the type or
`log.debug(..., exc_info=True)`. The `fetch_base` rungs in particular should
distinguish "library missing" from "extraction failed".

### M-6: 118 function-level imports, several covering dependency cycles (M)

Evidence: `grep -E "^\s{4,}(from|import) pka"` → 118 sites; heaviest in
`fetcher.py` (13), `providers/__init__.py` (12), `routers/runs.py` (9),
`domains.py` (8), `routers/ingestion.py` (8), `registry.py` (7). One cycle runs
through the shared ingestion tail, which **depends on tag training**:
`ingestion/core.py:ingest_text_block` → `clustering/doc_embeddings.py` →
`tag_training/lifecycle.apply_learned_tags_for_document` (also a performance
item, P-4). `api` imports `clustering`, which imports `tag_training`, which
imports `clustering`.

Recommendation: write the intended layering down (something like
`config/constants/db → storage → connectors → ingestion → clustering →
tag_training → api/cli`) and enforce it with
[import-linter](https://import-linter.readthedocs.io/) contracts run by
`ruff`'s sibling step in the verify table. Break the `ingestion → tag_training`
edge with a post-ingest hook registry (the runner calls
`on_document_embedded(doc_id)` listeners; tag training registers one). The
remaining lazy imports that exist only to defer heavy libraries (`umap`,
`hdbscan`, `torch`) are fine and should be commented as such.

### M-7: mypy is installed, undocumented, unconfigured, and red (S to start)

Evidence: `mypy` is in `[dev]`, README says "pytest, ruff, mypy", but there is
no `[tool.mypy]` section, it is absent from the CLAUDE.md verify table, and a
run yields 89 errors in 22 files (45 `arg-type`, 14 `assignment`, 8 `index`,
7 `call-arg`). The 7 `call-arg` errors on `TriggerRunRequest()` at
`routers/runs.py:226` are false positives from the missing pydantic plugin; the
45 `arg-type` ones are mostly `Row | None` flowing into `int`/`str` parameters
and deserve triage; some may be latent `None` bugs.

Recommendation: add

```toml
[tool.mypy]
python_version = "3.11"
plugins = ["pydantic.mypy"]
ignore_missing_imports = true
warn_unused_ignores = true
```

then ratchet: commit a baseline (`mypy … > mypy-baseline.txt` or the
`--enable-error-code`-per-module approach), add `mypy pka` to the verify table,
and forbid new errors. Do not aim for `strict`; the runners pass untyped dicts
by design.

### M-8: `Settings` is one 91-field class, the most-churned file, imported as a global by 31 modules (M)

> **Deferred to `BACKLOG.md` after costing — do not re-raise without reading
> `M8_NESTED_SETTINGS.md`.** Two corrections to the evidence below. (1) The
> "most-churned" signal is misread here: config.py's churn is 613 lines added
> against 96 deleted over 45 commits, median 8 per commit — *growth*, one
> setting per feature, not the rework that the churn × complexity heuristic in
> *References* is meant to flag. Nesting removes none of it. (2) Step 1 (the
> deprecated `class Config`) shipped under M-7. What survives costing is two
> narrow pieces — a shared `RemoteBackend` for the four duplicated remote
> backends, and subclassing `EnvSettingsSource` in `SecretsFileSettingsSource`
> — both recorded in `BACKLOG.md` under *Configuration*. Note also that this
> item bundles two complaints, and nesting addresses only the first: the
> process-wide singleton is what drives the 97 test monkeypatches, and it would
> need a `get_settings()` accessor or injection, not a namespace change.

Evidence: `pka/config.py`, 39 commits (highest in the repo), 91 fields in a
single `class Settings`, a deprecated inner `class Config` at line 491 (pydantic
emits `PydanticDeprecatedSince20` on every test run), and
`tests/conftest.py::isolated_settings` monkeypatches ~15 attributes on the
process-wide `settings` singleton for every test. The section banners inside
the class (`Source paths`, `Providers`, `Image gate`, `Ollama`, `Reddit`,
`Chunking`, `Firefox fetch`, `Retrieval enrichment`, …) are the split.

Recommendation, in two steps:

1. Replace `class Config` with `model_config = SettingsConfigDict(...)` (S,
   removes the warning).
2. Group into nested models (`settings.paths.*`, `settings.providers.*`,
   `settings.fetch.*`, `settings.clustering.*`) with `env_nested_delimiter`.
   This is a wide rename, so do it once, with `ruff`'s help and the
   `settings_view` tier map (`api/settings_view.py:137`) updated alongside. The
   payoff is that `conftest.py` pins a handful of sub-models instead of fifteen
   attributes, and `SettingsView`'s hand-maintained field-to-tier table gets its
   grouping from the model.

### M-9: `api/routers/ingestion.py` is five routers in one file (S)

Evidence: 515 lines, 19 commits, endpoints for status/progress/SSE, image
directory management, per-source path management, purge, domain top-lists,
unfetchable URLs, job control (start/pause/cancel/force), and vector rebuild,
plus the module-level worker registry and locks. Split into
`ingestion_status.py` (status, progress, SSE), `sources.py` (paths, dirs,
browse, purge), `ingestion_jobs.py` (sync/pause/cancel/rebuild + `_workers`).
`api/main.py` already registers routers from a list.

### M-10: frontend: hand-mirrored API types, no linter, `catch (e: any)` (S/M)

Evidence: `frontend/src/api/client.ts` is 580 lines with **49 interfaces**
mirroring 39 pydantic models, and is the fourth most-churned file (34 commits);
every schema change is edited twice with nothing checking they agree. There
is no ESLint/Prettier config; 14 `catch (e: any)` sites (`browse.ts`,
`TagTrainView.vue` ×5, …) all funnel into `notifyError(e: any)`. Vitest covers
`lib/` and `client.ts` only (46 tests); no store or component tests.

Recommendation:

- Generate `src/api/types.gen.ts` from FastAPI's `/openapi.json` with
  `openapi-typescript`, and have `client.ts` import from it. Add a check that
  the committed file matches a fresh generation (a pytest that dumps
  `app.openapi()` and diffs is enough, no running server needed).
- Add `eslint` + `@vue/eslint-config-typescript` with `no-explicit-any`; type
  the catches as `unknown` and narrow in `notifyError`.
- One store test (`stores/browse.ts` filter reducers) and one component test
  (`DocGridCard`) would establish the pattern; the `@vue/test-utils`
  dependency is already installed and unused.

### M-11: test suite: one 2,300-line file and 49 patches of private names (S)

Evidence: `tests/test_api.py` holds 161 tests across 13 classes; the section
banners (`Search`, `Documents`, `Clusters`, `Runs`, `Ingestion`, …) map 1:1 to
`tests/test_api_<router>.py` files sharing the app fixture via `conftest.py`.
Tests monkeypatch private attributes 49 times, concentrated on
`openlibrary._get_json` (16) and `EasyOcrProvider._reader` (11): those two are
de-facto seams and deserve a public injection point (a `client`/`reader`
constructor argument) so the tests stop coupling to names that a refactor will
rename.

Two deprecations the suite prints today are one-line fixes:
`routers/trends.py:62` `utcfromtimestamp` → `fromtimestamp(ts, UTC)`, and the
pydantic `class Config` above.

### M-12: no automated gate at all (S)

Evidence: no `.github/`, no pre-commit config, no `Makefile`/`check` script;
the verify table in CLAUDE.md is the only checklist. The current state is the
consequence: `ruff format --check` fails on one test file, the last recorded
coverage (84 %) is under the 85 % gate the project set for itself, and mypy has
never been run.

Recommendation: a single `scripts/check.ps1` + `scripts/check.sh` that runs
`ruff check`, `ruff format --check`, `mypy pka`, `pytest --cov`, and the two
`npm` commands, then a GitHub Actions workflow (or a pre-push hook) that runs
the same script. Because the repo is local-first, the workflow needs no
secrets; the test suite is fully mocked, and `tests/conftest.py` already makes
that a guarantee.

### M-13: small hygiene items (S, batchable)

- `pka/pipeline.py`: deprecated shim with **zero importers** in `pka/`,
  `tests/`, `scripts/`. Delete it and its `coverage.omit` line.
- vulture: `cli/purge_cluster_runs.py:25` unused `all_runs`;
  `connectors/reddit.py:447` unused `base_netloc`.
- `docs/ingestion-flows.md` and `docs/persisted-fields.md` rely on a manual
  "update in the same commit" rule that CLAUDE.md admits has no test. A cheap
  guard: a test asserting every string literal passed to `set_phase(...)` and
  every `documents` column name in `schema.py` appears verbatim in the
  respective doc. It cannot check the drawing is *right*, but it catches the
  common failure (a new phase or column nobody drew).
- The `scripts/run_*.py` shims are thin and fine; the `.bat`/`.ps1` launchers
  are the only place the 8420/8421 port split is enforced; one shared
  constant in `pka/constants.py`, read by `cli/dev.py` and documented once,
  would fix that.

## 4. Performance findings

### P-1: unindexed foreign keys under correlated `EXISTS` filters (S, highest ratio)

`pka/db/schema.py` declares five indexes (`documents.source/doi/arxiv_id/isbn`,
`chunks.document_id`) plus the `overlay_tags (document_id, tag, origin)` unique
index. **No index exists on**:

| Column | Hot reader |
|---|---|
| `source_tags.document_id`, `source_tags.tag_string` | `_where_source_tag` (`queries.py:912`), a correlated `EXISTS` per candidate row for every source-tag browse filter; `_browse_tag_maps`; `documents_out_batch`; `list_tags` |
| `source_collections.document_id` | `document_detail`, `_reddit_detail` |
| `cluster_assignments (run_id, document_id)` | `documents_out_batch:90`, `search.py:150`, `scatter_points`, `assign_new_docs`; every browse page and every search joins on it |
| `images.document_id` | `_exclude_pending_images` (`queries.py:962`), a correlated `EXISTS` applied to **every** browse list and count query |
| `fetch_log.document_id`, `reading_list_items (list_id, document_id)` | detail views, list views |
| `chunks (document_id, chunk_index)` | `_batch_first_chunk_map` (`queries.py:827`); an index would let SQLite find chunk 0 without sorting each document's chunks |

Without these, SQLite evaluates each `EXISTS` by scanning the child table per
outer row; the cost is `documents × source_tags` on a filtered browse. Add the
`sa.Index(...)` declarations to `schema.py` **and** the matching
`CREATE INDEX IF NOT EXISTS` lines to `init_db` (`queries.py:175-182` is the
pattern; `create_all` does not add indexes to tables that already exist).
Update `docs/persisted-fields.md` §1 if it lists indexes. Measure with
`EXPLAIN QUERY PLAN` on a `list_documents` call with one source tag before and
after; "SCAN source_tags" should become "SEARCH … USING INDEX".

### P-2: API cold start imports scikit-learn (S)

`python -X importtime -c "import pka.api.main"`: 4.7 s total, of which
`pka.api.routers.clusters` → `pka.clustering.engine` → `sklearn.decomposition`
is **3.0 s**. `engine.py:25-26` imports `PCA` and `normalize` at module level;
`tag_training/engine.py:11` does the same for `LogisticRegression`;
`storage/vector_store.py:33-36` imports chromadb eagerly. The routers already
lazy-import `run_clustering` inside handlers (`runs.py:220`), so the router
module just needs to stop importing `engine` at the top; the only module-level
use is `relabel_single_cluster` (`routers/clusters.py:27`), needed by one
endpoint.

Recommendation: move the sklearn imports inside the functions that use them
(`_run_pca`, `_normalize_for_cosine`, `train_session`), and make
`routers/clusters.py` import the engine lazily like `runs.py` does. Expected
result: sub-2-second `alexandria dev` restarts and a faster `--reload` loop.
This also shrinks the test-collection time, since `tests/test_api.py` imports
the app.

### P-3: clustering enumerates the whole Chroma collection to find document ids (S)

`_load_document_embeddings` (`engine.py:116`) starts with
`fetch_records(include=["metadatas"])`, a paged `collection.get` over **every
chunk in the archive**, purely to derive the set of document ids, even when
`load_cached_embeddings` then finds every vector in SQLite. And when *any*
document is missing its cached vector, the fallback fetches embeddings for
**all** `vector_ids`, not just the missing documents' chunks (`engine.py:157`).

Recommendation: source the candidate ids from SQLite
(`SELECT DISTINCT chunks.document_id JOIN documents … WHERE source IN (…)`,
served by `ix_chunks_document_id`), and for the missing set call the existing
`fetch_records_by_document_ids(missing, include=["embeddings"])`. Chroma is
then touched only for documents that actually lack a cached vector. The
BACKLOG item "clustering diagnostics are too slow" was the same shape of
problem and is checked off; this is its sibling in the run path.

### P-4: the shared ingest tail does ~6 round trips per document, twice for Calibre (M)

Per `ingest_text_block` call (`ingestion/core.py:28`): Chroma `upsert` (which
computes the embeddings) → SQLite `insert_chunks` (own transaction) →
`refresh_document_embedding` (`doc_embeddings.py:27`), which re-reads the
chunk ids from SQLite, does a Chroma `get(include=metadatas)`, a second Chroma
`get(include=embeddings)` for the vectors it just wrote, an `UPDATE documents`
transaction, and then `apply_learned_tags_for_document`, which opens another
transaction, selects every accepted tag-training model, and **unpickles each
model blob** (`_apply_model_to_documents`) to score one document. Calibre runs
the tail once per pass (metadata, then fulltext), so the mean-pool and the
learned-tag pass happen twice per book and the first result is discarded.

Recommendations, independently adoptable:

- Compute embeddings once in Python via the collection's embedding function
  and pass `embeddings=` to `upsert`; mean-pool the same array for
  `doc_embedding`. Removes both Chroma reads per document. (`vector_store`
  already exposes `_get_embedding_function()`.)
- Cache unpickled learned-tag models in `tag_training` keyed by
  `(session_id, updated_at)`; invalidate on accept/archive.
- Let runners defer the refresh: `ingest_text_block(..., refresh=False)` on
  pass 1 when a pass 2 will follow, or collect touched ids and refresh in one
  batch at the end of the sync phase (`refresh_document_embeddings(ids)` with a
  single `fetch_records_by_document_ids`).

### P-5: search: unbounded title scan and over-wide row fetch (S)

- Fulltext branch (`search.py:64-77`): `documents.title ILIKE '%q%'` with no
  `LIMIT` over the whole table on every fulltext/hybrid search and on every
  semantic search that returned nothing. BACKLOG already records that there is
  no FTS index anywhere; an **FTS5 virtual table over `title` +
  `card_summary`** (external-content, kept in sync by the `DocumentWrite`
  path) is the standard fix, and later allows keyword search over bodies
  via `chunks.text`.
- Filter step (`search.py:139`) and `documents_out_batch`
  (`document_serialize.py:65`) both `select(documents)`, every column
  including the 1.5 KB `doc_embedding` blob and `generated_summary`, for
  every hit, only to read `fetch_status`/`date_added`/card fields. Select the
  columns needed. Same applies to `document_detail` (`:239`), which is fine on
  a single row but sets the pattern.
- The semantic over-fetch `n_results=(offset+limit)*3` grows linearly with
  page depth; acceptable at current sizes, worth a ceiling.

### P-6: **withdrawn** — the SSE progress stream's source probes are already cached (none)

*Corrected 2026-09-03. As originally written this finding was wrong: it read
the call chain without reading the cache sitting in the middle of it, and its
line citations have since shifted. Kept rather than deleted so the numbering
stays stable for `TODO.md` / `BACKLOG.md` back-references.*

The call chain is real. `_progress_events`
(`pka/api/routers/ingestion.py:77`) does call `source_counts` every
`_COUNTS_INTERVAL_SECONDS = 1.0`, and `source_counts`
(`pka/ingestion/progress/baselines.py:186`) does call
`count_pending_metadata(src)`, whose per-source probes are genuinely
expensive — Firefox iterates the whole bookmark set
(`pka/ingestion/pending_metadata.py:124-127`), Calibre loads the full library
(`:138-143`), images walk the configured directories (`:145-157`).

But none of that runs at 1 Hz. `count_pending_metadata`
(`pending_metadata.py:108`) routes through `_cached_probe` (`:34`), which
memoises per `(kind, source)` for
`settings.ingestion_probe_cache_ttl_seconds` — **default 30.0**
(`pka/config.py:326`), the top of the range this finding went on to recommend
— and `invalidate_source_probes` (`:52`) clears it at job start, finish, and
purge. The same cache also backs the `load_firefox_bookmarks` /
`load_calibre_books` / `load_scanned_images` raw probes, which is exactly the
BACKLOG "source probes redundantly reload the same connector data"
duplication this finding cited as still open. It landed in `3180299`
(2026-08-17), a fortnight *before* the audited commit.

So the recommendation was already implemented when it was written, down to the
TTL value. Nothing to do. The one residual nit is cosmetic and belongs with
M-13: the comment above `_COUNTS_INTERVAL_SECONDS`
(`routers/ingestion.py:62-64`) says the counts "cost queries" without noting
that the expensive half is TTL-cached elsewhere, which is part of what made the
chain read as hot from the router end.

**Method note for the next audit.** This is the characteristic failure of
arguing from code shape alone: following a call chain top-down finds the
expensive leaf and stops, but reaching an expensive leaf does not prove it
*executes* at the caller's frequency. When a finding's premise is "X happens N
times per second", read X itself for memoisation before writing it up, and
`git log -S` the fix you are about to recommend to check it is not already in
the tree.

### P-7: `list_tags` sorts and limits in Python (S)

`queries.py:1143` runs two `GROUP BY` queries over the full `source_tags` and
`overlay_tags` tables, concatenates, sorts by count in Python and slices
`[:limit]`. Push the `ORDER BY n DESC LIMIT` into SQL (`UNION ALL` the two
selects, or two limited queries merged). With the P-1 indexes this becomes an
index-only aggregate.

### P-8: things checked and found fine (no action)

- 88 of 99 handlers are sync `def` and therefore run in Starlette's threadpool;
  the 11 `async def` ones (`status`, `progress`, SSE, pause/cancel) do only
  in-memory work or `run_in_threadpool`. Correct for a sync-SQLAlchemy app.
- Background sync/cluster work runs in daemon threads sharing one engine with
  `check_same_thread=False` + WAL; writes are short transactions. Fine for one
  user; note `_workers` (`ingestion.py:407`) never prunes finished threads.
- `sample_cluster_documents_for_clusters`, `_browse_tag_maps`,
  `documents_out_batch`, `load_cached_embeddings` are all batched.
- Frontend bundle: 138 KB main + 168 KB lazy `TrendsView` (chart.js), views
  are route-split, SSE with polling fallback. Nothing to do.

## 5. Test-suite health

- 1,418 tests pass in 261 s on Windows (WSL is roughly 3× faster per the dev
  notes). The slowest groups are `test_settings_view.py` (≈3 s **per test**,
  eight tests; they build a full report and probably a fresh `Settings()`
  plus provider construction each time, so a module-scoped report fixture
  would cut ~20 s) and `test_clustering.py` (≈2.6 s per test with UMAP/HDBSCAN
  mocked; worth a `--durations=0` look at what each `run_clustering` call is
  paying for, likely the sklearn import in P-2 plus real PCA on synthetic
  data).
- Coverage 84 % vs the 85 % gate. The low-coverage modules are the same three
  complexity hotspots (`engine.py` 62 %, `clustering/lifecycle.py` 63 %,
  `connectors/reddit.py` 59 %) plus `routers/runs.py` 69 % and
  `routers/tag_training.py` 58 %. M-1 and M-3 will make these testable at the
  unit level rather than through `run_clustering`.
- The mocks in `conftest.py` are thorough (Ollama, Chroma, HTTP, CLIP, umap,
  hdbscan via `sys.modules`), and the autouse settings fixture makes the
  "no network from a fresh checkout" promise testable. Keep it; M-8 makes
  it smaller.

## 6. Prioritised plan

Quick wins (an afternoon each, no design change):

1. P-1 indexes + `init_db` lines.
2. P-2 lazy sklearn/chroma imports.
3. M-12 `scripts/check.*` + M-7 mypy config with baseline; fix the format drift
   and the two deprecation warnings (M-11).
4. M-13 hygiene batch (delete `pipeline.py`, vulture items).

Medium (a focused day or two each):

6. M-3 split `search` + P-5 column selection; FTS5 as its own follow-up.
7. P-3 SQLite-sourced doc ids for clustering.
8. P-4 embed-once + batched refresh + model cache.
9. M-2 migrations list + `queries.py` split.
10. M-9 ingestion router split; M-10 generated API types + ESLint.

Larger (plan file each, per the `planning/` convention):

11. M-1 `clustering/engine.py` split with dataclass boundaries.
12. M-8 nested settings.
13. M-6 layering contract and the `ingestion → tag_training` hook.

## 7. Out of scope

Security was not reviewed (a separate `security-review` skill exists for
that). No benchmark was run against a real archive, so the P-items are ranked
by code shape and by what the schema makes SQLite do, with nothing timed.
P-1, P-3 and P-4 are the ones where a measurement on the
production database (read-only `EXPLAIN QUERY PLAN`, or timing one
`list_documents` and one clustering run) would confirm or demote them.

## References consulted

- [Codacy: Cyclomatic complexity guide](https://blog.codacy.com/cyclomatic-complexity) and [Code quality metrics](https://blog.codacy.com/code-quality-metrics): CC < 10 median, > 15 review; maintainability index as a refactor flag.
- [CodeAnt: Seven axes of code quality](https://www.codeant.ai/blogs/seven-axes-of-code-quality) and [Code quality metrics to track](https://codeant.ai/blogs/code-quality-metrics-to-track): churn × complexity as the refactor signal; duplication < 5–10 %.
- [Sonar: Cyclomatic complexity](https://www.sonarsource.com/resources/library/cyclomatic-complexity/).
- [FastAPI discussion #12089: async SQLAlchemy and the event loop](https://github.com/fastapi/fastapi/discussions/12089) and [FastAPI mistakes that kill your performance](https://dev.to/igorbenav/fastapi-mistakes-that-kill-your-performance-2b8k): sync-vs-async handler rules used in P-8.
- [Zestminds: FastAPI production issues](https://www.zestminds.com/blog/fastapi-production-issues-under-load/) and [Optimizing database queries in FastAPI](https://medium.com/@maheshwariaditya5555/optimizing-database-queries-in-fastapi-indexing-caching-and-pagination-caad1a320b96): N+1, indexes, pagination checklist used for P-1/P-5/P-7.
