# Ingestion progress tracking — refactor plan

**Status:** implemented 2026-08-19 against `trunk` @ d3c2a3a (uncommitted). Stages 0–6
are done; stage 7 was left alone (optional, different subsystem). Each stage below records
what actually shipped, including where it deviated from the plan.
**Scope:** `pka/ingestion/sync_progress.py`, `progress_baselines.py`, `pending_metadata.py`,
`loops.py`, `sync_helpers.py`, `runners/_common.py`, `pka/api/routers/ingestion.py`,
`frontend/src/stores/ingestion.ts`.

Reported symptoms: "many circular imports in ingestion progress tracking; it also seems
brittle and unresponsive". All three are real problems, but only two of them are the ones
named — see §2 before starting.

---

## 1. How to re-verify every claim here

Measurements below were taken read-only against the real `data/archive.db`
(210 documents / 4,278 chunks / 50 images, 21 MB).

```python
import sqlite3, time
con = sqlite3.connect("file:data/archive.db?mode=ro", uri=True)

def t(label, sql):
    s = time.perf_counter()
    r = con.execute(sql).fetchall()
    print(f"{(time.perf_counter()-s)*1000:8.1f} ms  {label} -> {r[:3]}")

for s in ("calibre", "firefox", "image", "reddit", "zotero"):
    t(f"embedded distinct-join [{s}]",
      "select count(distinct c.document_id) from chunks c "
      f"join documents d on c.document_id=d.id where d.source='{s}'")

for r in con.execute("select name, tbl_name from sqlite_master where type='index' "
                     "and tbl_name in ('chunks','documents','images')"):
    print("index:", r)
```

The import-cycle claim in §2 is reproducible with an AST walk over `pka/` that records
top-level vs. function-local `pka.*` imports, builds the top-level graph, and for each
deferred import asks whether hoisting it would close a cycle.

---

## 2. What is NOT the problem — do not chase this

**There are no circular imports in the ingestion progress path.** The AST import graph
over all of `pka/` finds **zero** top-level cycles. Every function-local import in the
progress path hoists cleanly to module level:

- `sync_progress` → `pka.constants`
- `sync_progress` → `pka.ingestion.pending_metadata`
- `loops` → `sync_progress`, `loops` → `sync_helpers`
- `runners/_common` → `sync_progress`, `runners/_common` → `sync_helpers`
- `sync_helpers` → `sync_progress`
- `fetcher` → `sync_progress`

The only genuine cycles in the repo are outside ingestion and out of scope:
`clustering.doc_embeddings` ↔ `tag_training.lifecycle`, `openlibrary` ↔ `book_search`,
`providers` ↔ `providers.vlm_ocr`.

**Two deferred imports must stay deferred** — they exist to avoid import cost, not cycles:

- `pending_metadata` → `image_pipeline` (pulls CLIP + the vector store)
- `registry.get_source_handlers()` → all six `*_sync` modules (lazy connector loading)

What the reporter is actually reacting to is a **layering** cycle (F1 below), which is why
deferred imports multiplied. Fixing the layering removes the need for them.

---

## 3. Diagnosis

### F1 — Bidirectional layering between state and data access

`sync_progress.py` is in-memory state, but `_to_dict()` reaches *down* into
`pending_metadata.metadata_job_progress()` (DB query + live source probe). Meanwhile
`progress_baselines.py` reaches back *up* into `sync_progress`. Neither module sits above
the other, so every call site defends itself with a function-local import.

Two files exist purely as deferred-import shims and should not survive the refactor:
`sync_helpers.py` (14 lines) and `runners/_common.py` (15 lines).

### F2 — Two sources of truth per progress bar

- **Metadata phase**: derived from live DB row counts *at serialize time*
  (`metadata_job_progress`). `run_metadata_loop` deliberately does **not** tick on success
  — its docstring says "progress is poll-driven (failure ticks only)". Close the browser
  tab and the metadata bar stops moving.
- **Fetching / embedding phases**: in-memory counters ticked by worker threads via
  `advance()`.

The two are reconciled by `_normalize_phases()` plus `_apply_monotonic_total()` /
`_apply_monotonic_processed()` clamps. Source-specific behaviour is hardcoded into the
state module in three places (`skip_embed = state.source == Source.FIREFOX` in
`_normalize_phases`, `_apply_db_counts`, and `begin_ingest`), and phase names are renamed
by alias maps `{"fulltext": "embedding", "ingesting": "embedding"}` duplicated across
`plan_pipeline`, `set_phase`, and `skip_phase`. This is the brittleness.

### F3 — Reads mutate state

`snapshot()` → `_to_dict()` → `_normalize_phases()` writes to shared state. Separately,
`GET /ingestion/sync/progress` calls `apply_progress_baselines()` as a side effect, so a
read endpoint advances server-side progress. Progress moves *because* a client polls.

### F4 — DB I/O under the global lock  ← the unresponsiveness

`snapshot()` holds `_lock` while `_to_dict()` → `metadata_job_progress()` →
`archive_document_count()` opens an engine connection and runs `COUNT(*)`. Every worker
thread's `advance()` blocks behind that query, at 2 polls/sec × N active sources.

### F5 — Poll amplification and duplicated work across two endpoints

`usePolling` fires every 500 ms (`POLL_MS = 500`) using `setInterval`, which does not wait
for the in-flight request; `api.syncProgress` passes timeout `0`, so a slow response never
aborts. Slow ticks stack.

Each tick issues one `/ingestion/sync/progress?source=` request per **active** source
(correct and deliberate — the UI routes `/ingestion/:source` and displays one source at a
time), **plus** `/ingestion/status`, which takes no source filter and loops all six
sources. For the displayed source the two endpoints compute the same rows twice:

| Query | `/sync/progress` | `/status` |
|---|---|---|
| `COUNT(*) FROM documents WHERE source=` | `phase_details.metadata.processed` | `by_source[src]` |
| `COUNT(DISTINCT chunks.document_id)` join | `phase_details.embedding.processed` | `fetch_by_source[src].embedded` |
| `fetch_status` GROUP BY | Firefox only, as phase `breakdown` | all sources |

`/ingestion/status` uniquely provides only three things for the displayed source:
`pending_metadata_by_source[src]` (cached source probe), the full `fetch_by_source[src]`
map for non-Firefox sources, and `source_unavailable[src]` (three filesystem/credential
stats). Its global `total` / `unfetchable` / `pending` fields are rendered **only** by the
`/ingestion` index metric cards, never by the source panel — so during a sync they are
computed twice a second and thrown away.

Call sites of `/ingestion/status`:

- `stores/ingestion.ts:101` in `load()` — sidebar mount, ingestion-page navigation,
  `savePath` / `addDir` / `removeDir` / `purge`. Infrequent, fine.
- `stores/ingestion.ts:138` in `pollProgress()` — **every 500 ms while any sync runs**.
  On a terminal tick it fires *twice*, since `load(true)` at lines 146/155/159 runs too.

### F6 — Missing indexes, an N+1, and an unguarded 298 MB copy

`sqlite_master` lists exactly two indexes across `documents`, `chunks`, and `images`:
`sqlite_autoindex_documents_1` and `sqlite_autoindex_images_1` — both implicit UNIQUE
indexes. **`chunks.document_id` and `documents.source` are unindexed**, so every embedded
count full-scans the chunks table:

| Query | Measured |
|---|---|
| `COUNT(DISTINCT chunks.document_id)` join, per source | **~16 ms** × 5 ≈ **80 ms** |
| `fetch_status` GROUP BY, per source | 0.1–4.7 ms |
| `COUNT(*) FROM chunks`, cold page cache | 222 ms |

≈100 ms warm per `/ingestion/status` call, twice a second during a sync — ~20% of a core,
on the FastAPI threadpool the sync worker also needs. Cost is **linear in chunk count**:
at 100k chunks that is ~400 ms per query, ~2 s per status call. That is the scaling cliff.

Two more spikes on the same path:

- **Zotero probe** (every 30 s, on `ingestion_probe_cache_ttl_seconds` expiry):
  `ensure_zotero_copy()` calls `copy_sqlite_database()` directly when `dev=False` (the
  default) — an unconditional full backup of the 298 MB `zotero.sqlite`, inside an HTTP
  request. It bypasses `ensure_sqlite_copy()`, the variant with the 60 s min-interval +
  mtime guard that `connectors/calibre.py:82` uses. Looks like an oversight.
- **Image probe**: `_image_already_indexed()` opens its own engine connection and runs one
  query **per image**. 50 images today; 10k images means 10k connections per probe.

---

## 4. Target architecture

**Rule: workers are the single writer; the API is a pure reader.** DB counts are the seed
at job start and the reconcile at job end — never a per-poll input.

```
pka/ingestion/progress/
  state.py      # SyncState, PhasePlan — stdlib only; no source-specific logic
  tracker.py    # registry + lock; write API (begin/advance/finish/...);
                #   snapshot() returns a frozen copy — no mutation, no DB
  view.py       # pure state -> dict (percent, clamping, phase visibility)
  baselines.py  # (was progress_baselines.py) DB -> counts; job start/finish only
```

Dependencies run strictly one way: `state ← tracker ← view / baselines ← api / runners`.
Nothing under `progress/` imports `pending_metadata`, `db`, or `connectors` except
`baselines.py`. Every deferred import listed in §2 hoists; `sync_helpers.py` and
`runners/_common.py` are deleted.

**Per-source phase spec.** Replace the hardcoded Firefox checks and the alias maps with a
declaration next to `SourceHandlers` in `registry.py` — which phases exist for a source and
which are tracked. `_normalize_phases` then becomes a trivial clamp with no source
knowledge.

**Transport: SSE** (decided — see §7). `GET /ingestion/sync/events?source=` streams
`text/event-stream`, one connection for the source being watched, events coalesced to
≤5/s. The existing `GET /ingestion/sync/progress` stays as the initial-state fetch and
fallback. Fold the three fields the panel needs from `/ingestion/status` into the event
payload so nothing polls that endpoint at all; it remains unchanged for the sidebar counts
and the index metric cards, called on mount and after actions.

---

## 5. Stages

Each stage is independently shippable and keeps `pytest` green. Run `pytest` after every
backend stage and `npm run build` in `frontend/` after stage 6.

### Stage 0 — Characterization tests ✅

Pin the current `snapshot()` dict for each source × each phase × each status, including the
Firefox `breakdown` and the metadata-job branch. These are the safety net for stages 3–5,
which deliberately change internals.
**Accept:** new tests pass against unmodified code.
**Shipped:** `tests/test_progress_contract.py` — ten scenarios, full-dict equality on
the serialized payload. All ten still hold after stages 1–5; the only payload that
changed at all is the metadata job's, and only because its numbers now come from ticks
(see stage 4).

### Stage 0.5 — Cheap wins on the hot path ✅

1. Add `Index("ix_chunks_document_id", "document_id")` and
   `Index("ix_documents_source", "source")` to `pka/db/schema.py`; confirm
   `alexandria init` stays idempotent.
2. Point `ensure_zotero_copy()` at `ensure_sqlite_copy()` so the non-dev path gets the
   60 s min-interval + mtime guard Calibre already uses.
3. Batch the image probe: one `WHERE path IN (...)` query instead of
   `_image_already_indexed()` per image.

**Accept:** the §1 timing script shows the distinct-join under ~1 ms per source; no
`zotero.sqlite` backup occurs during a status poll; the image probe issues one query.
**Shipped:** measured on a copy of the real `archive.db` (210 docs / 4,278 chunks), the
distinct-join went from **15.1–15.8 ms** to **0.02–0.37 ms** per source. The indexes are
declared in `schema.py` *and* created by `init_db()` — `create_all()` skips indexes on
tables that already exist, so an existing DB needs the explicit `CREATE INDEX IF NOT
EXISTS`. `ensure_zotero_copy()` now goes through `ensure_sqlite_copy`, and the image
probe is one `indexed_image_paths()` query.

### Stage 1 — Hoist the deferred imports ✅

Mechanical, no behaviour change. Hoist the six imports in §2; leave the two cost-motivated
deferrals alone.
**Accept:** the AST cycle check still reports zero cycles; `pytest` green.
**Shipped:** all six hoisted; the cycle check still reports zero.

### Stage 2 — Split into the `progress/` package ✅

Move code as-is into `state.py` / `tracker.py` / `view.py` / `baselines.py`. Keep
`sync_progress.py` as a re-export shim so the ~25 call sites don't move yet.
**Accept:** no logic diff; `pytest` green.
**Shipped — deviation:** no shim was kept. All ~25 call sites moved to
`from pka.ingestion import progress as sp` in the same pass, and `sync_progress.py`,
`progress_baselines.py`, `sync_helpers.py` and `runners/_common.py` were deleted. The
two optional-key helpers the shims existed for (`should_stop`, `tick`) live in
`tracker`.

### Stage 3 — Purity and the lock ✅  ← fixes the stalls

- `snapshot()` returns a frozen copy and performs no DB I/O and no mutation.
- Move `metadata_job_progress()` out of the serializer entirely: the worker writes an
  observed count into state; the view reads it.
- Never hold `_lock` across a DB call.

**Accept:** a test calling `snapshot()` concurrently with `advance()` shows no blocking; no
engine connection is opened from inside `tracker`.
**Shipped:** `tracker.snapshot_states()` returns deep copies under the lock; `view` then
serializes outside it and normalizes only its own copy. `metadata_job_progress` is gone
from `pending_metadata` entirely. Idle sources still need DB counts to draw their bars,
so `baselines.display_snapshot()` applies them **to the copy it is serializing** — a
read that writes nothing. `test_snapshot_opens_no_db_connection` pins the no-DB rule by
making `get_engine` raise.

### Stage 4 — Single writer ✅

Tick `run_metadata_loop` on every item, not just failures. Delete
`refresh_display_from_db`. `hydrate` runs at idle only. Remove the
`apply_progress_baselines()` side effect from the GET handler.
**Accept:** with the browser closed, a metadata sync's progress still advances (assert via
`snapshot()` in a test); the GET endpoint mutates nothing.
**Shipped:** `run_metadata_loop` ticks per item it acts on — *not* for items already in
`known`, nor ones `persist` reports as skipped, which the job's baseline already counts.
A metadata job whose probe underestimated `pending` now grows its total instead of
pinning at 100%. `refresh_display_from_db` is deleted; `hydrate` is ignored while a job
runs.

### Stage 5 — Per-source phase spec ✅

Add the phase declaration to `registry.py`. Delete `_normalize_phases`' Firefox branches,
both alias maps, `clear_embed_progress`, and `job_corpus_total`. Current API surface being
replaced: `skip_phase` ×13, `set_phase` ×8, `begin_metadata_sync` ×7, `set_corpus_total`
×6, `clear_embed_progress` ×4, `finish` ×4.
**Accept:** stage 0 characterization tests still pass (adjusted only where behaviour was
deliberately changed); `grep -rn "Source.FIREFOX" pka/ingestion/progress/` returns nothing.
**Shipped:** `PhaseSpec` in `registry.py` with two knobs — `plans_own_phases` (the
ingest discovers its work before setting totals) and `tracks_embedding` (false when
fetch and embed are interleaved). Firefox is the only non-default entry. The alias maps
turned out to have **no production callers** — only tests passed `"ingesting"` /
`"fulltext"` — so they are gone, and `set_phase` now raises `ValueError` on an unknown
phase instead of `KeyError`. `clear_embed_progress` (a no-op once every writer
normalizes) and `job_corpus_total` are deleted.

### Stage 6 — SSE transport ✅

Backend: `GET /ingestion/sync/events?source=` streaming coalesced snapshots, with the three
`/ingestion/status` fields folded into the payload. Frontend: replace `usePolling` with
`EventSource` in `stores/ingestion.ts`; delete the `ingestionStatus()` call at line 138;
keep `load()` for mount / navigation / post-action. Retain the GET endpoint for initial
state and as a fallback if `EventSource` fails.
**Accept:** during a sync the network panel shows one open stream and **no** repeating
`/ingestion/status` or `/ingestion/sync/progress` requests; completion toasts still fire
for a source the user has navigated away from; `npm run build` clean.
**Shipped:** `GET /ingestion/sync/events?source=` streams coalesced frames of
`{progress, counts}`, where `counts` is the per-source slice of `/ingestion/status`
(`baselines.source_counts`). The server closes the stream once the job reaches a
terminal state, so nothing idles on an open connection; a 15 s grace window covers the
gap between the client opening the stream and the POST that starts the job. The store
keeps one `EventSource` per *active* source (so background completions still toast) and
falls back to the old 500 ms polling if `EventSource` errors while a job is still
running. `npm run build` and the 40 frontend unit tests are clean. **Not verified in a
browser** — that needs the dev stack against real sources, which is the maintainer's
call to run.

### Stage 7 — Optional (not done)

Fold `pka/clustering/run_progress.py`'s cancel-token set into the same primitive as
`request_cancel` / `check_stop`.

---

## 6. Test surface and risk

**Final state:** 1,123 backend tests pass (`pytest`), `ruff check pka tests scripts` is
clean, and `npm run build` / `npm run test` are clean. Test modules were renamed with the
code: `test_sync_progress.py` → `test_progress_tracker.py`, `test_sync_helpers.py` →
`test_progress_stop.py`, plus the new `test_progress_contract.py`.

25 modules referenced `sync_progress`. Existing tests that needed attention:

| File | Lines |
|---|---|
| `tests/test_source_sync.py` | 422 |
| `tests/test_sync_progress.py` | 377 |
| `tests/test_progress_baselines.py` | 155 |
| `tests/test_ingestion_loops.py` | 82 |
| `tests/test_sync_helpers.py` | 28 (deleted with the shim) |
| `tests/test_api.py` | 1780 (ingestion endpoints only) |

Stages 0.5, 1, and 2 are mechanical. Stages 3–5 change observable progress semantics and
are the ones to review carefully. Stage 6 is the only frontend churn.

`tests/conftest.py` already redirects all data paths to `tmp_path` and mocks Ollama,
ChromaDB, HTTP, and CLIP — do not bypass those when adding fixtures for the new package.

---

## 7. Decisions already taken

- **SSE over hardened polling.** Chosen by the maintainer. The cheaper alternative (single
  request, real timeout, `setTimeout`-chained ticks) was considered and rejected.
- **Per-source request scoping is deliberate and stays.** The UI routes
  `/ingestion/:source` and shows one source; batching progress into one all-sources request
  would reintroduce the six-source cost that `?source=` exists to avoid. The store polls
  *active* sources rather than the displayed one so completion toasts still fire for a
  background sync — preserve that behaviour under SSE.
- **`/ingestion/status` keeps its unfiltered shape.** The sidebar needs all six `by_source`
  counts and the index page needs the global metrics. It is removed from the *poll* path,
  not changed.
