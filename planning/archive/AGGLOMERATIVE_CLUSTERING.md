# Two-level agglomerative clustering

**Status:** implemented (2026-09-02). `cluster_space="agglomerative"` in
`pka/clustering/engine.py`, wired through `TriggerRunRequest`, the CLI, and
`ClusterRunDialog.vue`. Tests in `tests/test_clustering.py`
(`TestRunClusteringAgglomerative`, `TestSplitSubtreeMatchesRebuild`,
`TestAutoKAgglomerative`, `TestAgglomerativeKCandidates`) and
`tests/test_api.py`. Archived here per `CLAUDE.md`'s planning-file convention.

**Revision (2026-09-02).** The question raised against the first draft: *does the
two-level pass re-run agglomerative clustering per L1 group? If so that is wasted
work, since L2 is only a cut in a tree we already built.*

Answer: yes, §2.4 as first written re-ran `linkage()` on each group's slice, and
that is redundant — but not for the reason assumed, and the fix carries a
consequence worth more than the saved time. §2.4 is rewritten; §6's cost table
is corrected against a measurement at real corpus size instead of an
extrapolation. In short:

- Re-running linkage on a group's slice returns the **identical partition** to
  cutting that group's subtree — verified, ARI = 1.0000 across ward, average and
  complete linkage. It is not an approximation of the tree cut; it is the same
  answer computed twice.
- The time saved is **small**: ~1.7 s of a ~20 s run at 17.9k docs, because L2
  cost is `Σ nᵢ²` ≈ 8% of `n²`. "Wasting a lot of time" overstates it. Reuse the
  tree for correctness and simplicity, not for speed.
- The consequence that actually matters: under one global tree, **L2 is by
  definition a deeper cut of L1**, so it cannot surface structure the L1 tree
  did not already contain. That is a real difference from HDBSCAN's L2 (local
  density re-estimation) and legacy's L2 (local UMAP), both of which recompute
  something. §5 has to compare like with like, and §2.4 names the one rebuild
  that would be justified if L2 turns out to be uninformative.

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
- **`Z` must escape.** Keeping `_run_hdbscan`'s labels-only return would force
  §2.4 to rebuild the tree it needs, which is the whole thing the revision
  removes. Either return `(labels, Z)` and let `_run_agglomerative_pipeline`
  carry `Z` to the L2 pass, or split the build (`_build_linkage`) from the cut
  (`_cut_linkage`) and have the pipeline hold `Z` between them. The second is
  cleaner: §2.2(c)'s sweep is then just repeated `_cut_linkage` calls, and the
  "build once, cut many times" structure is visible in the function names rather
  than implied by a comment. Downstream consumers still only ever see a label
  array, so §1's reuse table is unaffected.

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
tree built once (ward)                  3.20 s
8 candidate cuts (fcluster only)        0.04 s
silhouette scoring of those 8 cuts      2.23 s   → picked k=16 (true: 14)
```

Auto-selection therefore costs a little under one extra tree-build's worth of
time, and essentially all of it is the *scoring*, not the cutting — the cuts
themselves are 40 ms of the 2.27 s. (An earlier draft recorded 2.06 s / 1.14 s
and an exact recovery of k=14. Re-measuring gave the numbers above: slower on
both counts, and k=16 because the geometric grid `[8,10,12,16,20,25,32,40]` does
not contain 14 — silhouette picked the nearest candidate above the truth, which
is the grid's limitation, not a scoring failure. Worth knowing before §7 asserts
"recovers the planted count": that test only means anything if the planted count
is *on* the candidate grid.) Notes on making it robust:

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
2. Build the tree **once** and cut it → L1 labels; hold `Z` (§2.1).
3. `_run_level2_pass_agglomerative(..., Z=Z)` → L2 batches by cutting `Z`
   deeper inside each group (§2.4). `Z` is the only new value threaded through
   the pipeline.
4. `_label_l1_clusters(...)` — reused verbatim.
5. `_run_supervised_umap(pca_matrix, l1_labels, nn, min_dist)` — reused verbatim.

`n_neighbors` and `min_dist` stay meaningful: in the PCA path they only ever fed
the viz UMAP, and that is still true here.

`params` should record `cluster_space="agglomerative"`, `linkage`, the effective
`n_clusters` (or `distance_threshold`), `pca_components`, `pca_variance`,
`cluster_metric`, and `adaptive` — mirroring `_run_pca_pipeline`'s dict so
`/runs`' parameters column stays comparable across methods.

### 2.4 L2 by cutting the tree we already have

`_run_level2_pass_core` takes `compute_l2_labels(member_doc_ids, sub_mcs, sub_ms,
sub_nn)`. A new `_run_level2_pass_agglomerative` supplies a callback that
**cuts the L1 tree deeper inside the group**, ignoring `sub_ms`/`sub_nn` and
using only `len(member_doc_ids)` to pick the per-group count. The callback
signature does not change — this is precisely the seam
`_run_level2_pass_legacy` already uses to do something different. The two
HDBSCAN-flavoured guards in the core still read correctly and should be left
alone: `n_sub < sub_mcs` skips groups too small to subdivide, and
`len(l2_unique) < 2` drops a subdivision that produced only one cluster.

**Why not rebuild.** The first draft sliced the PCA matrix and called
`_run_agglomerative` again per group. That returns the *same partition* the tree
cut does, because an `fcluster` flat cluster is a contiguous dendrogram node, and
ward/average/complete cluster distances are functions only of the merged members
— so the standalone sub-dendrogram over a group **is** the induced subtree.
Measured on 3,000 points cut into 12 L1 groups, then each split to `k=4`:

| linkage | L1 build | L2 by rebuild | L2 by tree cut | ARI(rebuild, cut) |
|---|---|---|---|---|
| ward | 0.88 s | 68.1 ms | 12.2 ms | **1.0000** |
| average | 0.81 s | 65.6 ms | 14.0 ms | **1.0000** |
| complete | 0.69 s | 66.1 ms | 14.0 ms | **1.0000** |

Rebuilding with identical parameters is therefore strictly dominated: same
answer, more code, more time. But note the size of the win — L2 cost is
`Σ nᵢ²`, roughly 8% of `n²` for a dozen balanced groups, so at 17.9k docs this
saves ~1.7 s out of ~20 s (§6). Reuse the tree because it removes a redundant
code path and makes L1/L2 nesting exact by construction, not because it is fast.

**Implementation.** scipy has no "extract subtree as a linkage matrix" API, and
none is needed:

1. `leaders(Z, l1_labels)` returns the dendrogram node id backing each flat L1
   cluster — verified to match the group's member set exactly.
2. Split that node into `kᵢ` parts with a max-heap on merge height: push the
   node, repeatedly pop the highest-distance node and push its two children until
   `kᵢ` pieces remain. That is `fcluster(..., "maxclust")` restricted to the
   subtree, in ~20 lines and O(kᵢ) per group.

The auto-`k` sweep of §2.2(c) applies per group unchanged, and gets cheaper: the
candidate cuts are now ~1 ms each, so per-group sweep cost is entirely the
silhouette scoring.

**Constraint this introduces.** Cutting by height is only meaningful on a
**monotone** linkage. `ward`, `average`, `complete` and `single` are monotone;
`centroid` and `median` admit inversions, which break both `fcluster`'s maxclust
criterion and the heap split. Restrict the `--linkage` / API choices to the
monotone four (§4) rather than passing scipy's full method list through.

**The one rebuild that would be justified.** Because L2 is now definitionally a
deeper cut, it cannot find structure the global tree missed. The global 50 PCs
are chosen to explain *between*-cluster variance; within one L1 cluster, the
distinctions that matter may live in directions PCA discarded. If §5's
evaluation shows L2 labels are uninformative, the fix is not "rebuild the tree
on the same slice" (that changes nothing) but **re-run PCA within the group and
build a fresh tree in that local space** — the agglomerative analogue of what
`_run_level2_pass_legacy` does with local UMAP. That costs `Σ nᵢ²` linkage plus a
small PCA per group and is the only version where the word "re-run" earns its
keep. Do not build it now; note it as the escape hatch.

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
| `pka/api/schemas/clusters.py` | `TriggerRunRequest.cluster_space` pattern `^(pca\|legacy_umap\|agglomerative)$`; add `linkage` (`^(ward\|average\|complete\|single)$` — monotone only, per §2.4), `n_clusters` (`ge=2`), `distance_threshold` (`gt=0`); reject *both* being set with a model validator (neither set is legal — that is the §2.2(c) auto sweep) |
| `pka/api/routers/runs.py` ~200 | `algorithm = ALGORITHM_LEGACY if … else ALGORITHM_PCA` is a two-way ternary — replace with a `{cluster_space: algorithm}` dict lookup before it grows a third arm |
| `pka/cli/clustering.py` ~51 | `--cluster-space` `choices` gains `agglomerative`; add `--linkage` (`choices=["ward","average","complete","single"]`), `--n-clusters`, `--distance-threshold`; add a usage line to the docstring |
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
- **L2 informativeness — new, and specific to this method.** Per §2.4, L2 here
  is a deeper cut of the L1 tree, whereas HDBSCAN's L2 re-estimates density
  locally and legacy's re-runs UMAP locally. So judge agglomerative L2 on its
  own terms: do the sub-labels name real distinctions, or do they read as
  arbitrary slices of a coherent parent? If the latter, that is the signal to
  reach for §2.4's local-PCA escape hatch — not a reason to reject the method at
  L1, where the coverage argument stands independently.
- **Runtime**: wall-clock via the existing `_StepTimer` entries.

If agglomerative loses on cohesion at equal cluster counts, the method stays in
the codebase as a comparison option and HDBSCAN remains the default — that is a
valid outcome, not a failed task.

## 6. Risk: O(n²) at this corpus size

~17.9k chunked documents. Measured on this machine (random 50-d float32,
`method="ward"`), **including a direct run at corpus size** rather than the
earlier extrapolation:

| n | linkage time | `fcluster` cut | condensed `pdist` |
|---|---|---|---|
| 4,000 | 0.70 s | 1.0 ms | 0.064 GB |
| 8,000 | 3.20 s | 1.7 ms | 0.256 GB |
| **17,879** (measured) | **20–22 s** | **3–4 ms** | **1.28 GB** |

The first draft extrapolated ~11–15 s at 17.9k from an 8k data point; the real
figure is ~20 s, so the extrapolation was optimistic by roughly 1.5×. The run
completed without OOM, which answers the headroom question §6 originally
deferred — 1.28 GB (`n(n-1)/2` float64, since `pdist` always returns float64) is
a survivable transient on this machine.

Note the cut column: once `Z` exists, every L1 candidate cut and every L2
subdivision is milliseconds. That is what makes both the §2.2(c) sweep and
§2.4's tree reuse essentially free, and it means the whole method costs one
`linkage()` call.

Time is a non-issue — 20 seconds sits well inside a pipeline that already spends
minutes on LLM labelling. **Memory is the real constraint**, and it applies to
*every* linkage method, not just the expensive ones: `scipy.cluster.hierarchy.
linkage` starts with `y = distance.pdist(y, metric)`, materialising the full
condensed matrix before `nn_chain` ever runs. Ward has no memory advantage here.
(An earlier draft of this plan claimed ward was O(n) memory via nn-chain from an
observation matrix. That is wrong — checked against the scipy source.)

1.28 GB is a transient allocation on top of the PCA matrix and is survivable, but
it is the number that decides whether this scales. Consequences:

- ~~Verify headroom before building anything else.~~ Done — the 17,879-row
  `linkage()` above ran to completion. Re-check on the real PCA matrix only if
  the corpus grows materially.
- It grows quadratically, so a 2× larger archive is ~5 GB and stops fitting.
  That is the point at which this needs a kNN `connectivity` graph (sklearn's
  `AgglomerativeClustering` supports it and makes the problem sparse) — at the
  cost of giving up the reusable scipy tree that §2.2's auto-`k` **and now
  §2.4's L2** both depend on. That tension is sharper after the revision: the
  sparse fallback would force L2 back to per-group rebuilds. Note it; do not
  pre-build the mitigation.
- A corpus-size guard that refuses with a clear message beats an OOM mid-run.

L2 costs nothing at all after the §2.4 revision: it is a heap walk over an
existing tree, milliseconds per group. (Had it rebuilt per group, cost would be
`Σ nᵢ²` — ~8% of the L1 matrix for a dozen balanced groups, so ~1.7 s here, not
the ~2% the first draft claimed.)

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
  a synthetic fixture can assert honestly. **Put the planted count on the
  candidate grid**, or the test measures grid spacing rather than the sweep (see
  the k=16/k=14 note in §2.2).
- The sweep's candidate range is clamped to the corpus: a 40-doc fixture must not
  propose `k=40`.
- **L2 is an exact refinement of L1** (§2.4): every L2 cluster's members share
  one `parent_cluster_id`, and the union of a parent's L2 clusters is exactly its
  L1 membership. Free to assert under tree reuse, and the assertion is what
  catches a regression back to a rebuild that drifts.
- **Tree cut ≡ rebuild**, as a unit test on the split helper alone: for a small
  matrix, `leaders`-plus-heap-split of a group must equal
  `fcluster(linkage(slice), k, "maxclust")` up to label permutation. This is the
  claim the whole §2.4 revision rests on; measured at ARI 1.0 for ward/average/
  complete, so it should be pinned rather than trusted.
- A non-monotone linkage (`centroid`, `median`) is rejected at the edge with a
  422 rather than silently producing an inverted tree.
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
