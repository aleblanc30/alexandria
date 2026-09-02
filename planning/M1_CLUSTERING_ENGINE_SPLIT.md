# M-1: split `pka/clustering/engine.py`

Plan for the audit's top-priority item (`planning/MAINTAINABILITY_PERFORMANCE_AUDIT.md`
§M-1). Not started — this file records the split and the order of operations before
touching code.

## Why now

`engine.py` is 2,073 lines (was 1,620 at audit time; the agglomerative-clustering
feature landed since and added ~450 more without changing the module's shape).
`run_clustering` now takes 15 keyword parameters across three pipeline branches
(`legacy_umap`, `pca`, `agglomerative`), each of which builds and destructures its
own positional tuple. The seams the audit called out (`# ── Step N` banners) still
hold and are now more obviously overdue to become real module boundaries.

## Target layout

| New module | Moves from `engine.py` |
|---|---|
| `clustering/types.py` | `ALGORITHM_PCA` / `ALGORITHM_LEGACY` / `ALGORITHM_AGGLOMERATIVE`, `L2ClusterBatch`, `ClusterRunResult`, `_StepTimer`, new `ClusterParams` / `PipelineOutput` dataclasses (see below) |
| `clustering/embeddings.py` | `_mean_pool_from_chroma`, `_load_document_embeddings` (Step 1) |
| `clustering/reduce.py` | `_run_pca`, `_run_supervised_umap`, `_run_umap_legacy` (Steps 2–3) |
| `clustering/hdbscan_step.py` | `adaptive_cluster_params`, `_normalize_for_cosine`, `_run_hdbscan`, `_ADAPTIVE_MIN_CLUSTER_SIZE`, `_ADAPTIVE_MAX_CLUSTER_SIZE` (Step 4) |
| `clustering/agglomerative.py` | `_build_linkage`, `_cut_linkage`, `_run_agglomerative`, `_agglomerative_k_candidates`, `_pick_best_k`, `_auto_k_agglomerative`, `_subtree_leaves`, `_split_subtree`, `_split_node_auto` (Step 4b — a peer of `hdbscan_step.py`, not part of the orchestrator) |
| `clustering/labelling.py` | Step 5 in full: `DocSample`, `_format_doc_sample_lines`, `_regenerate_prompt_suffix`, `_json_response_hint`, `_label_via_chat`, `_label_cluster_with_llm`, `_label_parent_from_children_with_llm`, `_TFIDF_STOPWORDS`, `_tfidf_label_from_strings`, `_tfidf_label`, `_label_one_cluster`, `_label_clusters`, `_label_l1_clusters`, `_label_cluster_from_docs`, `_label_l1_db_cluster_from_children`, `relabel_single_cluster`, `relabel_run_clusters` (~475 lines on its own) |
| `clustering/persist.py` | `create_run_placeholder`, `set_run_status`, `_cluster_centroid_blob`, `_write_hierarchical_clusters`, `_build_umap_records`, `_commit_run`, `_finalize_run`, `_persist_run`, `_build_cluster_docs` (Step 6) |
| `clustering/engine.py` (stays, shrinks to orchestrator) | `_run_level2_pass_core`, `_run_level2_pass`, `_run_level2_pass_legacy`, `_run_level2_pass_agglomerative`, `_run_pca_pipeline`, `_run_legacy_pipeline`, `_run_agglomerative_pipeline`, `_spawn_async_relabel`, `run_clustering` |

No import cycles: `engine.py` imports from all seven; `labelling.py` and
`persist.py` are the only ones that import `pka.db.queries` / `pka.db.schema`;
`agglomerative.py` and `hdbscan_step.py` are pure-numpy siblings with no
cross-imports between them.

## Two structural changes, not just a file move

1. **Replace tuple returns with `PipelineOutput`.** `_run_pca_pipeline` returns a
   10-tuple, `_run_legacy_pipeline` a 9-tuple, `_run_agglomerative_pipeline` its own
   shape — `run_clustering` destructures each positionally
   (`engine.py:1890` onward). Collapse all three to one
   `@dataclass PipelineOutput` (labels, reduced_2d, label/desc maps, l2 batches,
   n_l1, n_noise, l2_noise, extra params dict). `run_clustering` then picks a
   pipeline function and reads named fields instead of branching on tuple length.
   `ClusterRunResult` and `L2ClusterBatch` already show this pattern is accepted
   here.

2. **Group knobs into `ClusterParams`.** `run_clustering` is now at 15 keyword
   parameters (`min_cluster_size`, `min_samples`, `n_neighbors`, `min_dist`,
   `n_components`, `pca_components`, `label_model`, `skip_labelling`,
   `async_labelling`, `cluster_space`, `linkage`, `n_clusters`,
   `distance_threshold`, `source_filter`, `run_id`), each hand-mirrored across
   `TriggerRunRequest` (`api/schemas/clusters.py`), the CLI's `run_kw` dict
   (`cli/clustering.py`), and `lifecycle.run_incremental_clustering`'s
   `**run_kwargs` passthrough. `min_dist=0.1` and `n_components=5` are declared
   as defaults in three separate places today. Introduce
   `ClusterParams` (dataclass, same fields) in `clustering/types.py`; have
   `TriggerRunRequest.to_params()` build one, and `run_clustering(params:
   ClusterParams, *, run_id=None)` take it instead of 13 loose keywords.
   `source_filter` and `run_id` are per-call context, not tuning knobs — keep
   those as separate arguments, not fields on `ClusterParams`.

## Call-site and test fallout to fix in the same change

- **`tests/test_clustering.py`**: ~35 call sites do
  `run_clustering(min_cluster_size=2)` etc. — becomes
  `run_clustering(ClusterParams(min_cluster_size=2))`. Grep
  `run_clustering(` before starting to get an exact count post-rebase.
- **Patch targets that move modules**: confirmed via
  `grep -n "pka\.clustering\.engine\." tests/*.py` — only two cross into
  `labelling.py`:
  - `tests/test_api.py:1092` → `pka.clustering.labelling._label_cluster_with_llm`
  - `tests/test_clustering.py:1378` → `pka.clustering.labelling._label_parent_from_children_with_llm`
  - `tests/test_clustering.py` also patches `engine.cfg` and
    `engine.sample_cluster_documents_for_clusters` directly (not via string path)
    for the cancel-during-labelling regression test — that test imports
    `from pka.clustering import engine` and monkeypatches attributes on the
    module object, so it must follow `_label_clusters` to `labelling.py` and
    patch `labelling.cfg` / `labelling.sample_cluster_documents_for_clusters`
    instead.
  - All `run_clustering` patches (`test_api.py:1353,1368,1414,1517`,
    `test_clustering.py:1324`) stay at `pka.clustering.engine.run_clustering` —
    it doesn't move.
  - No test currently patches the agglomerative helpers by string path, so
    `agglomerative.py` extraction has no patch-target fallout.
- **`pka/api/routers/runs.py`**, **`pka/cli/clustering.py`**,
  **`pka/clustering/lifecycle.py::run_incremental_clustering`**: all three build
  a kwargs dict/bag for `run_clustering` today; update each to build a
  `ClusterParams` instead. `TriggerRunRequest` already validates
  `n_clusters` xor `distance_threshold` — that validator moves with it, unaffected.
- **`pka/cli/clustering.py`**'s `run_kw` dict and `TriggerRunRequest`'s fields must
  stay in sync with `ClusterParams`' fields; once `ClusterParams` exists, argparse
  can build one directly instead of going through a dict.

## mypy baseline

`pka.clustering.engine` is in `pyproject.toml`'s `[[tool.mypy.overrides]]`
(baseline-ratcheted list, M-7). Its only real error is
`res.inserted_primary_key[0]` (untyped `Result`) at what is currently three call
sites (`create_run_placeholder`, `_finalize_run`'s `_write`, `_persist_run`'s
`_write`) — all three move to `persist.py`. Fix the type there (annotate or cast
the `Result`) and drop `pka.clustering.engine` *and* skip adding
`pka.clustering.persist` / the other five new modules to the override list —
shrinking the baseline per the file's own instruction ("Shrink this list as a
module gets cleaned up; do not add to it").

## Order of operations

1. `clustering/types.py` first (dataclasses only, no logic) — everything else
   imports from it.
2. Pure/leaf modules next, in any order: `embeddings.py`, `reduce.py`,
   `hdbscan_step.py`, `agglomerative.py` — each is a straight cut-and-paste plus
   import fixes, verified independently by running the relevant
   `tests/test_clustering.py` subset.
3. `persist.py` (fix the `inserted_primary_key` type while it's isolated).
4. `labelling.py` (repoint the two string-path monkeypatches and the `engine.cfg`
   attribute patches in the same commit).
5. Introduce `PipelineOutput` and `ClusterParams` in `types.py`, thread them
   through the three pipeline functions and `run_clustering`, and update every
   call site listed above.
6. Full `pytest` + `mypy pka` + `ruff check` pass; update the mypy override list.
7. Check off M-1 in `planning/TODO.md` under "Maintainability & performance" with
   a summary line, same style as the existing P-1/M-7/M-13 entries.

## Not in scope

- `docs/ingestion-flows.md` / `docs/persisted-fields.md` — this is a pure
  refactor, no pipeline phase shape, outbound call, or persisted column changes.
- Behavior changes to any pipeline (PCA/legacy/agglomerative numerics stay
  byte-for-byte identical — only where the code lives and how params are passed
  changes).
- M-2 (`db/queries.py` split) and M-3 (`search` route split) — separate audit
  items, tracked independently in `planning/TODO.md`.
