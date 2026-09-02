# Cluster diagnostics: precompute at run end

Plan for `TODO.md` → *Clustering* → **"Clustering diagnostics are too slow"**.

The TODO's diagnosis ("they are only computed when requested") is right about
the symptom and half right about the fix. Computing them at the end of the run
is the correct move for the expensive half; the other half — drift — is
*defined* relative to "now" and is exactly zero at run end, so freezing it there
would not make it fast, it would make it meaningless. This plan splits the two.

## 1. Where the time actually goes

`GET /runs/{id}/diagnostics` (`pka/api/routers/runs.py:123`) does four things:

| Step | Cost |
|------|------|
| cluster sizes — one `GROUP BY` on `cluster_assignments` | SQL, negligible |
| `_n_noise` — two `COUNT(DISTINCT …)` | SQL, negligible |
| `compute_drift(run_id)` | **one full centroid pass** |
| `compute_merge_suggestions(run_id)` | **a second full centroid pass** |

Both of the latter call `_get_cluster_centroids(run_id, level=1)`
(`pka/clustering/lifecycle.py:105`), which:

1. reads every level-1 assignment for the run — every clustered document;
2. hands that whole id list to `_doc_mean_embeddings`, i.e.
   `fetch_records_by_document_ids(all_doc_ids, include=["embeddings"])`;
3. mean-pools **chunk** embeddings back up to documents in Python.

So a single diagnostics request pulls every chunk vector of the entire clustered
archive out of Chroma **twice** — at 17.9k documents that is hundreds of
thousands of 384-d vectors per pass, decoded through Chroma's `$in`-batched
`get`, then re-mean-pooled per document in a Python loop. Both passes then throw
the result away; the next request repeats it.

Three separate wastes, in descending order:

- **It recomputes what the run already had.** `run_clustering` holds
  `doc_ids, matrix` — the mean-pooled per-document matrix, in the same 384-d
  space, in memory (`engine.py:1461`). Centroids are a `np.add.reduceat` away.
  Recomputing them later from raw chunks is redoing step 1 of the pipeline.
- **It ignores the SQLite cache.** `documents.doc_embedding` already stores the
  mean-pooled document vector, and `load_cached_embeddings`
  (`clustering/doc_embeddings.py:74`) reads it in 5000-id batches. It is not a
  stale side-cache: `refresh_document_embedding` is called on the ingestion tail
  for every document (`pka/ingestion/core.py:108`), and both the clustering
  engine (`engine.py:149`) and tag training (`tag_training/engine.py:141`) read
  through it, falling back to Chroma only for the ids it reports missing.
  `lifecycle.py` is the sole holdout — `_doc_mean_embeddings` goes to Chroma
  unconditionally and re-does the pooling the cache already holds.
- **It does the pass twice per request**, because drift and merges each call
  `_get_cluster_centroids` independently.

`assign_new_docs` (`lifecycle.py:161`) pays the same cost twice more, once for
L1 and once for L2.

## 2. The shape of the fix

**Persist the centroids; keep the cheap parts on demand.**

A centroid is a fact about the run: it is fixed the moment the run's assignments
are written, and it is the only expensive input to both drift and merges. Once
it is on disk:

- **merge suggestions** become an `n×n` matmul over ≤ ~50 L1 centroids — under a
  millisecond, no reason to persist the suggestions themselves;
- **drift** becomes an embedding fetch for *only the documents ingested since
  the run* (usually zero, occasionally a few hundred), served from
  `documents.doc_embedding`;
- **`assign_new_docs`** stops re-deriving centroids on every incremental update.

Storing the derived *suggestions* instead would be the wrong cut: merge
suggestions are cheap given centroids, and drift is not storable at all.

### 2.1 A note on drift semantics

`compute_drift` measures documents with `ingested_at > run_ts` against their
cluster centroid. At the instant the run finishes that set is empty, so every
drift score is `0.0` and every `flagged` is `False`. Precomputing drift at run
end therefore yields a table of zeros. Drift stays on demand.

There is one deliberate behavioural change: today's centroid is recomputed from
current membership, so documents added by `assign_new_docs` are folded into the
centroid they are then measured against — drift dilutes itself as it grows. A
persisted centroid is the run's centroid, and drift measures departure from it.
That is the measure the docstring already claims ("drift *from* the run"), so
this is a correctness fix riding along, not a regression — but it will change
existing drift numbers, so expect `flagged` to trip slightly more readily.
Nothing acts on that flag: `run_incremental_clustering` reports it and stops,
per `DESIGN.md` §4.

## 3. Storage

Add one column:

```python
sa.Column("centroid", sa.LargeBinary),   # clusters, float32 blob, 384-d
```

on `clusters` in `pka/db/schema.py`, mirroring `documents.doc_embedding` and
reusing `embedding_to_blob` / `blob_to_embedding` from
`clustering/doc_embeddings.py`.

Why a column on `clusters` rather than a `cluster_centroids` table or a
`cluster_runs.diagnostics` JSON blob:

- it is one row per cluster, per run, with the label right beside it — every
  consumer already selects from `clusters` filtered by `run_id` and `level`, so
  the centroid rides along on a query that is already being made;
- **purge is free.** `purge_cluster_run` (`pka/cli/purge_cluster_runs.py:34`)
  deletes `clusters` rows for the run; the centroids go with them. A side table
  would need a new delete and a new count in `_count_run_rows`, and both are the
  kind of thing that gets forgotten;
- it works unchanged for L2, which `assign_new_docs` needs;
- a JSON blob on `cluster_runs` would have to be re-parsed and re-keyed to db
  cluster ids on every read, and would go stale against `relabel_run_clusters`.

Migration in `init_db` next to the existing cluster migrations
(`pka/db/queries.py:135`):

```python
if cl_cols and "centroid" not in cl_cols:
    con.execute(sa.text("ALTER TABLE clusters ADD COLUMN centroid BLOB"))
```

Nullable, so every pre-existing run keeps working via the fallback in §4.3.

## 4. Implementation

### 4.1 Write centroids during the run

`_write_hierarchical_clusters` (`engine.py:873`) already owns the doc→cluster
mapping and the `_insert_cluster` call that mints each db id, and it runs inside
the same transaction as the `status="finished"` update in `_finalize_run` — so a
centroid written here is committed atomically with the run being marked
finished, which is exactly what the TODO asks for.

- thread `matrix: np.ndarray` and a `doc_index: dict[int, int]` (built once from
  `doc_ids`) through `_commit_run` into `_write_hierarchical_clusters`;
- in `_insert_cluster`, pass `centroid=embedding_to_blob(mean_vec)` where
  `mean_vec` is the mean of `matrix[[doc_index[d] for d in members]]`;
- L1 members come from `_build_cluster_docs`-style grouping over `l1_labels`;
  L2 members come from `batch.doc_ids` + `batch.labels`, which index the same
  `matrix` rows via `doc_index`.

Cost: one `np.mean` per cluster over rows already in RAM — microseconds, well
inside the noise of the run it rides on. No new phase, no new progress event, no
network call, nothing that can fail and abort a completed run.

Keep `matrix` and `doc_index` **optional** on those helpers (default `None` →
write `centroid=None`) so `relabel_*` paths and any future caller that does not
hold the matrix are unaffected.

### 4.2 Read them back

Add to `lifecycle.py`:

```python
def load_persisted_centroids(run_id: int, level: int = 1) -> dict[int, np.ndarray]:
    """{db_cluster_id: centroid} from clusters.centroid; missing rows omitted."""
```

and make `_get_cluster_centroids` try it first, falling back to the existing
Chroma path only when the run has **no** persisted centroids at all (a
pre-migration run). Partial coverage should not trigger a full recompute — a
cluster with a null centroid simply drops out, the same way a cluster whose
documents have no embeddings drops out today.

Backfill: when the fallback fires, write what it computed back into
`clusters.centroid`, so a legacy run pays the old cost exactly once. This makes
the migration a no-op for the user — no `alexandria` subcommand to remember, no
re-run required.

### 4.3 Cheap drift

In `compute_drift`, replace `_doc_mean_embeddings(all_recent)` with
`load_cached_embeddings(all_recent)` and fall back to `_doc_mean_embeddings`
only for the ids it reports missing. `all_recent` is already scoped to documents
ingested after the run, so this is small by construction.

`_doc_mean_embeddings` stays — it is still the fallback and still what
`assign_new_docs` uses for unassigned documents. It should get the same
cache-first treatment there (`load_cached_embeddings` → Chroma for the
remainder), which is a two-line change in the same spirit.

### 4.4 One centroid load per request

`run_diagnostics` currently calls `compute_drift` and
`compute_merge_suggestions` back to back, each loading centroids. Give both an
optional `centroids: dict[int, np.ndarray] | None = None` parameter and have the
router load once and pass it to both. Existing callers (CLI, tests,
`run_incremental_clustering`) keep working unchanged.

### 4.5 Not changing

- `DiagnosticsOut`, the endpoint path, `api/client.ts`, `stores/clusters.ts`,
  `RunManagerView.vue` — the response shape is identical, so the UI is untouched.
  If §5 shows the endpoint is now fast enough, a follow-up can load diagnostics
  automatically when a run finishes instead of behind the *Diagnostics* button;
  that is a UX change, out of scope here.
- `docs/ingestion-flows.md` and `docs/persisted-fields.md`: neither covers the
  clustering tables (persisted-fields mentions clustering only as an
  `overlay_tags.origin` value, §93), and nothing about ingestion phases or
  per-source writes changes. No doc sync required — worth stating explicitly so
  the next reader does not go looking.

## 5. Verification

- `pytest tests/test_clustering.py tests/test_api.py` — the existing drift and
  merge tests (`test_clustering.py:479-561`) must pass unchanged; they assert
  shape and thresholds, not exact scores, so the §2.1 semantics change should
  not move them. If one does move, it is measuring the dilution bug.
- New tests:
  - `_write_hierarchical_clusters` writes a non-null `centroid` per L1 and L2
    cluster, and the value round-trips through `blob_to_embedding` to the mean
    of that cluster's rows in `matrix`;
  - `_get_cluster_centroids` on a run with persisted centroids makes **no**
    Chroma call (assert against the `fetch_records_by_document_ids` mock in
    `conftest.py`);
  - a run whose `clusters.centroid` is all-null falls back, returns the same
    centroids as before, and has backfilled the column afterwards;
  - `purge_cluster_run` leaves no centroid rows behind (implied by the existing
    clusters delete — one assertion, cheap insurance against a later refactor
    moving centroids to a side table without a delete).
- Manual: on the real archive, time `GET /runs/{id}/diagnostics` before and
  after. Expected: two full-archive Chroma embedding passes → one indexed
  `SELECT` over ≤ ~50 rows.

## 6. Out of scope

- **"Suggested merges in cluster diagnostics cannot be performed"** — the
  sibling TODO item. It needs a `POST /runs/{id}/merge` and a UI affordance, and
  it reads centroids that this plan makes cheap, so it lands more easily after
  this. Separate change.
- Recomputing centroids after `assign_new_docs` — see §2.1; the run's centroid
  is deliberately the stable reference.
