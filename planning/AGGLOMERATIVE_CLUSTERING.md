# Two-level agglomerative clustering

**Status:** needs revision — not implemented.
**Moivation for revision:** does the 2 level clustering iteratively rerun agglomerative clustering? If so, we are wasting a lot of time, since l2 clustering only requires a cut in an already built tree. We need to evaluate the tradeoff here.

`pka/clustering/engine.py` only ever runs HDBSCAN. `cluster_space` picks the
*feature space* it runs in (`pca` → PCA 50d, cosine; `legacy_umap` → UMAP → HDBSCAN,
euclidean), but the clusterer is the same both ways. This plan adds a genuinely
different algorithm — agglomerative (hierarchical) clustering — as a third
selectable mode, reusing the existing L1/L2 machinery rather than rebuilding it.

**Why bother.** HDBSCAN is density-based: it refuses to place low-density points
and dumps them in noise. On this archive that is most of the corpus — run #4
reports **17,879 unassigned** of 21,412 documents. Agglomerative clustering
partitions *everything*, is deterministic (no `random_state`), and takes a direct
"how many clusters do you want" knob instead of the indirect
`min_cluster_size`/`min_samples` pair. It is a different set of trade-offs, not a
strict improvement — which is why it is a run option to compare against, not a
replacement. Run acceptance (§4 of `DESIGN.md`) is how the two get compared.

## 1. What already exists and is reused unchanged

The two-level structure is **not** new. Only the clusterer is.

| Piece | Where | Reused how |
|---|---|---|
| L1/L2 persistence | `_write_hierarchical_clusters` (engine.py ~859) | As-is. Writes `clusters.level` (1/2) and `parent_cluster_id`. |
| L2 orchestration | `_run_level2_pass_core` (engine.py ~944) | As-is, via its `compute_l2_labels` callback seam. |
| Labelling | `_label_clusters`, `_label_l1_clusters`, TF-IDF fallback | As-is — takes `{cluster_id: [doc_ids]}`, algorithm-agnostic. |
| 2D scatter | `_run_supervised_umap` (engine.py ~211) | As-is — takes labels, does not care who produced them. |
| Doc embeddings | `_load_document_embeddings`, `_run_pca` | As-is. |
| Cosine trick | `_normalize_for_cosine` (engine.py ~301) | As-is — L2-normalize, then euclidean. |
| Run lifecycle | `pka/clustering/lifecycle.py` | Untouched. Assignment is nearest-centroid and already algorithm-agnostic (no `ALGORITHM_*` reference in the file). |

The work is therefore one new clusterer function, one new pipeline function, and
plumbing to select it.

## 2. Where it plugs in

### 2.1 `_run_agglomerative` — new, next to `_run_hdbscan`

Mirrors `_run_hdbscan`'s contract exactly: takes a matrix, returns an
`np.ndarray` of integer labels per row. That identical contract is what makes
every downstream consumer work untouched.

```
_run_agglomerative(reduced, n_clusters=None, distance_threshold=None,
                   *, linkage_method="ward", metric="cosine") -> np.ndarray
```

**Use scipy, not `sklearn.cluster.AgglomerativeClustering`.** This is the one
implementation choice that matters, and §2.2 is why: scipy exposes the
dendrogram as a reusable object (`scipy.cluster.hierarchy.linkage` → `Z`, then
`fcluster(Z, k, criterion="maxclust")`), whereas sklearn's estimator hides it and
refits from scratch for every `n_clusters`. Cutting a prebuilt tree at a
different `k` costs **~1 ms**; refitting costs seconds. Every other decision here
follows from keeping that tree.

- `metric="cosine"` reuses `_normalize_for_cosine` then runs euclidean, exactly
  as `_run_hdbscan` does — same helper, same justification (equivalent to cosine
  on unit vectors, and `ward` *requires* euclidean).
- `distance_threshold` is the same tree cut with `criterion="distance"`, so both
  stopping rules come free from one `Z`. Exactly one may be set — validate at the
  edge (§4) so a bad combination is a 422, not a `failed` run row with a
  stringified exception in `notes`.
- **Emits no `-1`.** See §3.

Both scipy and sklearn are already hard dependencies, unlike the `hdbscan` /
`umap-learn` optional imports.

### 2.2 How the cluster count is picked

Three ways, in priority order. The default is the third.

**(a) Explicit.** The user sets `n_clusters` in the dialog or `--n-clusters` on
the CLI. Always wins.

**(b) By distance.** The user sets `distance_threshold` and the count falls out
of where the dendrogram is cut. Structure-driven, but the right threshold is
corpus- and embedding-dependent, so it is an expert knob, not a default.

**(c) Auto — silhouette sweep over the prebuilt tree.** Build `Z` once, cut it at
each candidate `k`, score each cut, keep the best.

Do **not** reuse `adaptive_cluster_params`' `target_clusters` for this, which was
the obvious move and is wrong. That expression is
`max(4, min(12, round(n_docs**0.5)))`, and the `min(…, 12)` pins it at **12 for
any corpus over 144 documents**:

| n_docs | 100 | 144 | 500 | 5,000 | 17,879 | 21,412 |
|---|---|---|---|---|---|---|
| `target_clusters` | 10 | 12 | 12 | 12 | 12 | 12 |

It is not a cluster-count heuristic at all — it is an intermediate term for
deriving `min_cluster_size = n_docs // (target_clusters * 2)`, which is why that
value grows to 744 on this archive (matching run #3's stored `min_cluster_size:
745`). Borrowing it would hand every corpus a constant `k=12`.

The sweep is affordable precisely because of §2.1. Measured on 8,000 synthetic
50-d points with 14 planted clusters:

```
tree built once (ward)                  2.06 s
sweep of 9 candidate k, silhouette      1.14 s   → picked k=14 (true: 14)
```

Auto-selection therefore costs roughly one extra tree-build's worth of time and
recovered the planted count exactly. Notes on making it robust:

- Score with `sklearn.metrics.silhouette_score(..., sample_size=3000,
  random_state=…)`. Silhouette is O(n²) at full size; the `sample_size` argument
  is what keeps each candidate cheap, and it is the difference between a 1-second
  sweep and a multi-minute one.
- Candidate range should be anchored to a target L1 cluster size rather than a
  fixed list — something like `k ∈ [8, 40]` for a corpus this size, geometrically
  spaced. Keep L1 in a range a human can browse; L2 is where fine structure goes.
- Silhouette on high-dimensional embeddings is a weak absolute signal but a
  reasonable *relative* one across cuts of the same tree. Record the winning
  score and the full sweep in `params` so a bad auto-pick is diagnosable from the
  `/runs` parameters column rather than invisible.
- Ties and near-ties: prefer the smaller `k` (coarser, more browsable).

This keeps the dialog's existing **Auto-tune** checkbox meaningful for the new
method: checked → sweep; unchecked → the explicit `n_clusters` field. The run row
records `adaptive` exactly as today.

For **L2**, the same sweep runs per L1 group on that group's own small tree — a
few hundred points each, so the cost is negligible (§6).

### 2.3 `_run_agglomerative_pipeline` — new, next to the other two

Same shape and return tuple as `_run_pca_pipeline`, so `run_clustering`'s
if/elif needs one more branch and nothing else:

1. `_run_pca(matrix, pca_components)` — reused verbatim.
2. `_run_agglomerative(pca_matrix, n_clusters=k, linkage=…)` → L1 labels.
3. `_run_level2_pass_agglomerative(...)` → L2 batches (§2.4).
4. `_label_l1_clusters(...)` — reused verbatim.
5. `_run_supervised_umap(pca_matrix, l1_labels, nn, min_dist)` — reused verbatim.

`n_neighbors` and `min_dist` stay meaningful: in the PCA path they only ever fed
the viz UMAP, and that is still true here.

`params` should record `cluster_space="agglomerative"`, `linkage`, the effective
`n_clusters` (or `distance_threshold`), `pca_components`, `pca_variance`,
`cluster_metric`, and `adaptive` — mirroring `_run_pca_pipeline`'s dict so
`/runs`' parameters column stays comparable across methods.

### 2.4 L2 via the existing callback seam

`_run_level2_pass_core` takes `compute_l2_labels(member_doc_ids, sub_mcs, sub_ms,
sub_nn)`. A new `_run_level2_pass_agglomerative` supplies a callback that slices
the PCA matrix and calls `_run_agglomerative` with a per-group cluster count
derived from `len(member_doc_ids)`, **ignoring** `sub_ms`/`sub_nn`. The callback
signature does not change — this is precisely the seam
`_run_level2_pass_legacy` already uses to do something different.

Two guards in the core are HDBSCAN-flavoured but read correctly for
agglomerative too, and should be left alone: `n_sub < sub_mcs` skips groups too
small to subdivide, and `len(l2_unique) < 2` drops a subdivision that produced
only one cluster.

## 3. The one real semantic difference: no noise

HDBSCAN emits `-1`; agglomerative assigns every document. Consequences, all
benign, but each should be *checked* rather than assumed:

- `_write_hierarchical_clusters` and `_run_level2_pass_core` both compute
  `set(labels) - {-1}` — correct when no `-1` is present.
- `_n_noise` (`pka/api/routers/runs.py` ~72) is `total_chunked - assigned`, so it
  goes to 0 on its own. No change needed.
- `run_diagnostics` and the Noise metric card on `/runs` will read 0. That is the
  honest answer for a partitioning algorithm, not a bug — but it removes the main
  signal the diagnostics panel currently carries, so §5's comparison is what the
  run is actually judged on.
- `_run_supervised_umap` passes labels as `y`; umap treats `-1` as unlabelled, so
  a fully-labelled `y` is simply fully supervised. Fine.
- `compute_drift` / `compute_merge_suggestions` operate on stored assignments and
  are unaffected.

## 4. Plumbing (the small, mechanical part)

`cluster_space` becomes a three-valued **pipeline mode** rather than strictly a
"feature space" — agglomerative also runs on PCA output, so the name is a little
off. Accept the naming debt: `archive/CLUSTER_RUN_DIALOG.md` §1 deliberately shaped the
"Method" dropdown so a new algorithm lands as a data change, the UI already says
*Method*, and a separate orthogonal `cluster_algorithm` param would create an
agglomerative × legacy_umap cross-product nobody wants.

| File | Change |
|---|---|
| `engine.py` ~44 | `ALGORITHM_AGGLOMERATIVE = "agglomerative-hierarchical"` |
| `engine.py` `run_clustering` ~1457 | Third branch on `space`; new `linkage` / `n_clusters` / `distance_threshold` kwargs (all `None`-defaulted, so every existing caller is unaffected) |
| `engine.py` module docstring | It documents the pipeline step-by-step and currently names only two paths |
| `pka/api/schemas/clusters.py` | `TriggerRunRequest.cluster_space` pattern `^(pca\|legacy_umap\|agglomerative)$`; add `linkage`, `n_clusters` (`ge=2`), `distance_threshold` (`gt=0`); reject *both* being set with a model validator (neither set is legal — that is the §2.2(c) auto sweep) |
| `pka/api/routers/runs.py` ~200 | `algorithm = ALGORITHM_LEGACY if … else ALGORITHM_PCA` is a two-way ternary — replace with a `{cluster_space: algorithm}` dict lookup before it grows a third arm |
| `pka/cli/clustering.py` ~51 | `--cluster-space` `choices` gains `agglomerative`; add `--linkage`, `--n-clusters`, `--distance-threshold`; add a usage line to the docstring |
| `pka/config.py` ~423 | The `cluster_space` comment lists valid values; add `cluster_linkage: str = "ward"` if a config-level default is wanted |
| `frontend/src/api/client.ts` | `ClusterRunParams`: widen `cluster_space`, add the three fields |
| `frontend/src/components/ClusterRunDialog.vue` | Third `<option>`; the PCA-components / UMAP-dims block is currently a binary `v-if`/`v-else` on `method === 'pca'` and needs restructuring into per-method field groups |

## 5. Success criteria

**The baseline is fixed.** `adaptive_cluster_params` used to back-solve
`min_cluster_size` from a `target_clusters` pinned at 12, which made it scale
~linearly with corpus size — 744 (with `min_samples=372`) on this archive, dense
enough that HDBSCAN called ~83% of it noise. It now derives `min_cluster_size`
from `sqrt(n_docs)` capped at 50, so the default HDBSCAN run itself assigns far
more of the corpus without any manual tuning. Run a fresh clustering run and use
*that* as the baseline for comparison, not run #3/#4, which predate the fix.

Compare via `/runs` diagnostics plus a scratch script:

- **Coverage**: agglomerative assigns 100% by construction; record what fraction
  HDBSCAN assigns on the same corpus under the corrected default (run #4, under
  the pre-fix heuristic: ~17% — not a fair comparison point any more).
- **Cohesion**: mean intra-cluster cosine similarity, and silhouette score on the
  PCA matrix — computed for both methods over *assigned* documents only, so
  HDBSCAN is not penalised for the documents it declined.
- **Label quality**: eyeball the L1/L2 labels in the cluster explorer. A method
  that assigns everything but produces incoherent grab-bag clusters has bought
  coverage with meaninglessness, which the numeric scores above will partly but
  not fully catch.
- **Runtime**: wall-clock via the existing `_StepTimer` entries.

If agglomerative loses on cohesion at equal cluster counts, the method stays in
the codebase as a comparison option and HDBSCAN remains the default — that is a
valid outcome, not a failed task.

## 6. Risk: O(n²) at this corpus size

~17.9k chunked documents. Measured on this machine (scipy 1.17.1, random 50-d
points, `method="ward"`):

| n | linkage time | condensed `pdist` |
|---|---|---|
| 2,000 | 0.10 s | 0.016 GB |
| 4,000 | 0.46 s | 0.064 GB |
| 8,000 | 2.19 s | 0.256 GB |
| **17,879** (extrapolated, n^2.2) | **~11–15 s** | **~1.28 GB** |

Time is a non-issue — 15 seconds sits well inside a pipeline that already spends
minutes on LLM labelling. **Memory is the real constraint**, and it applies to
*every* linkage method, not just the expensive ones: `scipy.cluster.hierarchy.
linkage` starts with `y = distance.pdist(y, metric)`, materialising the full
condensed matrix before `nn_chain` ever runs. Ward has no memory advantage here.
(An earlier draft of this plan claimed ward was O(n) memory via nn-chain from an
observation matrix. That is wrong — checked against the scipy source.)

1.28 GB is a transient allocation on top of the PCA matrix and is survivable, but
it is the number that decides whether this scales. Consequences:

- **Verify headroom before building anything else.** Run one `linkage()` on the
  real PCA matrix in a scratch script and watch RSS. The table above is measured
  at 8k and extrapolated to 17.9k; the extrapolation is the part to confirm.
- It grows quadratically, so a 2× larger archive is ~5 GB and stops fitting.
  That is the point at which this needs a kNN `connectivity` graph (sklearn's
  `AgglomerativeClustering` supports it and makes the problem sparse) — at the
  cost of giving up the reusable scipy tree that §2.2's auto-`k` depends on.
  Note the tension now; do not pre-build the mitigation.
- A corpus-size guard that refuses with a clear message beats an OOM mid-run.

L2 is cheap regardless: it runs per L1 group, so cost is `Σ nᵢ²` over groups —
for 12 groups of ~1,500 that is ~2% of the L1 matrix, which is what makes a
per-group `k` sweep affordable.

## 7. Tests — `tests/test_clustering.py`

The existing suite mocks `hdbscan` and `umap` as fake modules (`_mock_hdbscan`,
`_mock_umap`) because both are optional heavyweight imports. **Agglomerative
needs no mock**: it is sklearn, already a hard dependency, and the fixtures run
real `PCA` on real fake embeddings today. Running the real clusterer over the
40-doc fixture is both simpler and a stronger test.

- A `populated_agglomerative` fixture (fake Chroma + `_mock_umap` for the viz +
  `_mock_llm`, no clusterer mock), then the `TestRunClustering` assertions
  re-run against `cluster_space="agglomerative"`: run row persisted, cluster rows
  written, L2 rows carry `parent_cluster_id`, assignments cover every doc.
- **No `-1` anywhere** in `cluster_assignments` for such a run, and
  `n_noise == 0` — the §3 invariant, asserted rather than assumed.
- `n_clusters=k` yields exactly `k` L1 clusters (given `k` ≤ fixture size).
- The §2.2(c) auto sweep picks the planted count on a fixture built from a known
  number of well-separated centres — the property that actually matters, and one
  a synthetic fixture can assert honestly (the 8k benchmark above recovered 14/14).
- The sweep's candidate range is clamped to the corpus: a 40-doc fixture must not
  propose `k=40`.
- `tests/test_api.py` — `cluster_space="agglomerative"` reaches `run_clustering`;
  `n_clusters=1` and the both-set/neither-set combinations return 422.

## 8. Docs

- **`DESIGN.md` §4** — currently states flatly that "Clustering uses
  **hierarchical HDBSCAN** (PCA space by default)". Rewrite as: two-level
  hierarchical clustering with a selectable clusterer, HDBSCAN (default) or
  agglomerative, noting the noise-vs-full-partition trade-off.
- **`docs/ingestion-flows.md` and `docs/persisted-fields.md` are unaffected** —
  clustering is not an ingestion flow, and no column changes meaning or
  ownership. (`archive/CLUSTER_RUN_DIALOG.md` §5 reached the same conclusion for the same
  reason; the `CLAUDE.md` sync rule is about ingestion writes.)
- Mark the `planning/TODO.md` Clustering entry done and set this file's status.
