# Cluster run parameter dialog

**Status:** implemented.

`+ New run` on `/runs` fires a clustering run with zero configuration. Everything
the pipeline can be tuned with is reachable from the CLI (`alexandria clustering
--min-cluster-size 10 --n-neighbors 20 --cluster-space legacy_umap`) but not from
the UI, so the only way to try different parameters is to drop to a shell.

This plan puts a modal between the button and the run. **Scope is the dialog and
its plumbing only** — it exposes parameters `run_clustering()` already accepts. No
new clustering algorithm is added here; the method dropdown is shaped so one can
be added later as a data change rather than a rewrite.

## 1. What the dialog exposes

Every control maps to an existing `run_clustering()` keyword
(`pka/clustering/engine.py`), already offered by `pka/cli/clustering.py`.

| Control | Param | Default | Notes |
|---|---|---|---|
| Method | `cluster_space` | `pca` | `pca` (PCA → HDBSCAN, cosine, supervised UMAP viz) or `legacy_umap` (UMAP → HDBSCAN) |
| Auto-tune parameters | — | on | When checked, sends `null` for the three fields below |
| Min cluster size | `min_cluster_size` | adaptive | |
| Min samples | `min_samples` | adaptive | |
| Neighbours | `n_neighbors` | adaptive | |
| Min distance | `min_dist` | `0.1` | |
| PCA components | `pca_components` | `50` (`cfg.cluster_pca_components`) | shown only when method = `pca` |
| UMAP dims | `n_components` | `5` | shown only when method = `legacy_umap` |
| Skip LLM labelling | `skip_labelling` | off | TF-IDF labels only |
| Label in background | `async_labelling` | off | TF-IDF first, LLM relabel after |

**Auto-tune** is one checkbox rather than three "auto" toggles because that is
exactly how the engine behaves: `None` means "derive from document count" via
`adaptive_cluster_params()`, and the run row records
`params["adaptive"] = min_cluster_size is None`. Checked reproduces today's
behaviour exactly.

Deliberately **out of scope**: `label_model` (a config concern, not a per-run
knob) and `source_filter` (wants a multi-select against the source list — its own
piece of UI). Both are accepted by `run_clustering()` and can be folded in later.

## 2. Backend

### 2.1 `pka/api/schemas/clusters.py`

New `TriggerRunRequest`. All fields optional; numeric fields carry `ge`/`le`
bounds so a bad value returns 422 at the edge instead of raising inside the
background thread, where the only trace is a `failed` run row with a stringified
exception in `notes`.

### 2.2 `pka/api/routers/runs.py`

`trigger_run` (line ~179) takes the model as an optional request body and
forwards the fields to `run_clustering`.

The three current query params (`skip_labelling`, `async_labelling`,
`cluster_space`) are superseded by body fields. The only callers are our own UI
and `tests/test_api.py`, which posts with no body — an omitted body keeps every
default, so those tests pass untouched.

### 2.3 `pka/clustering/engine.py` — `create_run_placeholder()`

Line ~822 hardcodes `algorithm=ALGORITHM_PCA` and `parameters="{}"`. That is
invisible today because the UI cannot select anything else, but once the dialog
offers `legacy_umap` a running row would display the wrong algorithm until it
finishes and `_finalize_run` overwrites it. Pass algorithm and the chosen
parameters into the placeholder insert.

## 3. Frontend

1. **`components/ClusterRunDialog.vue`** — new. No modal exists in the codebase
   yet, so this sets the pattern: `<Teleport to="body">`, overlay-click and
   Escape to dismiss, focus moved into the first field on open, `submit` emits
   the parameter object.
2. **`styles/global.css`** — `.modal-overlay` / `.modal` / form-field classes,
   built on the existing `--surface` / `--border` / `--radius-lg` tokens.
3. **`api/client.ts`** (line ~282) — exported `ClusterRunParams` type;
   `triggerRun(params?)` sends a JSON body, `{}` when called bare.
4. **`stores/clusters.ts`** (line ~97) and **`views/RunManagerView.vue`**
   (line ~5) — `trigger(params?)` passes through; the button opens the dialog
   instead of firing the run, and the dialog's `submit` calls
   `store.trigger(params)`.

## 4. Tests

- `tests/test_api.py` — body reaches `run_clustering` with the expected kwargs
  (patch it and assert the call), out-of-range values return 422. The
  no-body-means-defaults case is already covered around line 1281.
- `frontend` — a `client.ts` test for body serialisation. vitest only globs
  `src/lib`, `src/api`, `src/constants`, so the `.vue` component itself is
  covered by `npm run build`'s `vue-tsc` pass, not by a unit test.

## 5. Docs

One line in the `DESIGN.md` clustering section noting the run parameters are
settable from the UI. `docs/ingestion-flows.md` and `docs/persisted-fields.md`
are unaffected — clustering is not an ingestion flow, and no column changes
meaning or ownership.
