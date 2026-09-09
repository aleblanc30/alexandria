# Maintainability & performance audit, 9 September 2026

Audit of Alexandria v0.0.11 at commit `2732be4`. Static analysis plus the test
suites; **no profiling against a real archive** was done (CLAUDE.md forbids
running real ingestion), so every performance finding argues from code shape
rather than from a timing. Where a number would change the priority, the finding
says what to measure. The one exception is §5's test-suite finding, which *was*
measured — with a fake, not a real archive, so no rule was bent.

This is a proposal document like the rest of `planning/`: nothing here is
authoritative about current behaviour.

**Numbering.** This refreshes `MAINTAINABILITY_PERFORMANCE_AUDIT.md` (2 September,
commit `e732b91`) and **continues its sequence** — `M-14` onward, `P-9` onward —
rather than restarting at 1. The old document's `M-1`…`M-13` / `P-1`…`P-8` are
still referenced from `TODO.md` and `BACKLOG.md`; reusing those ids here would
make every back-reference ambiguous. Read the two documents together.

**Scope exclusion, agreed before the run.** `M-2` (`db/queries.py` split), `M-9`
(ingestion router split), `M-6` (layering contract) and `P-5`'s FTS5 index are
reported as done in the maintainer's working copy, which is not reachable from
the commit audited here. Their current shape in this tree is recorded in §2 for
the record, and they are **not** re-filed as findings. Everything else is
measured against `2732be4` as it stands.

## 1. Method

The usual audit axes for a Python/TypeScript service (complexity, module size and
cohesion, coupling, duplication, dead code, test health, typing, exception
hygiene, tooling) and the FastAPI performance checklist (blocking work on the
event loop, N+1 and unindexed queries, over-fetch, startup cost, background-work
lifecycle). Audit tools were installed into a scratch `--target` directory;
`pyproject.toml` and the lockfile are untouched.

| Check | Result |
|---|---|
| `ruff check pka tests scripts` (project rules) | clean |
| `ruff format --check` | clean, 256 files |
| `ruff check pka --select C901,PERF,SIM,PLR09,ARG,RET,TRY,BLE,S110,S112` | 189 findings (TRY003 70, PLR0913 40, C901 16, ARG001 14, PLR0911 10, PLR0912 7, PLR0915 5). **BLE001/S110/S112 now 0** — M-5 shipped |
| radon cyclomatic complexity | 16 functions at CC ≥ 11 of 1,110 blocks; average A (4.02); worst: `_fetch_one_impl` **51 (F)**, `init_db` 47 (F), `parse_arxiv_atom` 23, `run_clustering` 23, `assign_new_docs` 20 |
| radon maintainability index | no C-grade modules; `tag_training/lifecycle.py` 16.53 (B), `db/queries.py` 15.41 (B). `clustering/engine.py` was **5.6 (C)** in September and is now above the B threshold |
| `mypy pka` (project config) | clean, 159 files |
| `mypy pka` with the override list removed | **76 errors in 19 files** (was 89 in 22) |
| vulture ≥ 70 % confidence | 4 items, all pydantic-validator `cls` false positives |
| pylint `duplicate-code`, ≥ 10 lines | 6 duplicated blocks, all in the publisher/fetch-handler family |
| `python -X importtime -c "import pka.api.main"` | **0.89 s** cold import (was 4.7 s); sklearn, chromadb, torch, umap, hdbscan all absent from `sys.modules` afterwards |
| `pytest --durations=25` (runnable subset, this container) | 1,568 passed, 5 skipped, **82.2 s** |
| Same subset with the per-domain rate limit collapsed | 1,560 passed, **41.0 s** — see M-15 |
| Coverage | **not measurable here** — see §5 |
| `npm run lint` / `test` / `build` | clean / 77 passed in 9 files / builds in 3.4 s |

Size, for scale: backend 20,112 SLOC (14,195 logical) across 159 modules; tests
19,494 SLOC in 84 files (≈1:1 with the code); frontend 6,778 lines of TS/Vue
hand-written, plus 4,090 generated (`src/api/types.gen.ts`).

**One method limitation to record.** The clone this ran against was shallow;
`git fetch --unshallow` recovered the full 203-commit history, so the churn
figures below are real. But seven test modules cannot execute in this container
(`test_fetcher.py`, `test_vector_store.py`, `test_book_extractor.py`,
`test_reddit_sync.py`, `test_image_pipeline.py`, `test_image_extractor.py`,
`test_image_gate.py`) for reasons that predate this commit: missing `torch`, a
stubbed `chromadb`, and async tests that hang. Every number drawn from the suite
is therefore a subset measurement and is labelled as such.

## 2. Headline

Six of the previous audit's items have shipped, and the measurements confirm it
rather than taking the changelog's word for it. Cold API import is **0.89 s**,
down from 4.7 s, with no scientific library resident afterwards (P-2). Blind
`except Exception` is down from 49 to **zero unannotated** under a lint that now
fails on new ones (M-5). `clustering/engine.py` has left the C grade entirely
(M-1). `mypy` is clean under its ratchet (M-7), the check script exists (M-12),
and the frontend has a linter, generated API types and its first store and
component tests (M-10). Duplication is 6 blocks in one family, dead code is 4
false positives, and the `/search` route is no longer the worst function in the
tree.

The problems are again concentrated, and in fewer places than last time:

1. **The fetch dispatcher has absorbed every new handler.** `_fetch_one_impl` is
   CC **51** — nearly double the 26 the last audit recorded, and now the worst
   function in the codebase. The publisher-handler plan shipped; the chain it
   fed grew. (M-14)
2. **Half the test suite's wall time is a real sleep.** The per-domain rate
   limiter has no test hook, so the fetch-handler tests wait on it: 82 s becomes
   41 s with the gap collapsed. (M-15)
3. **One missing composite index turns a bulk tag apply into a per-cluster scan
   of the whole run.** (P-9)
4. **The mypy ratchet still freezes 20 modules**, which is the shape M-7 chose
   deliberately, but the list has barely moved. (M-16)

### Excluded items, current shape in this tree

Recorded so the next run can tell "not audited" from "not done". None of these
is a finding here.

| Item | Shape at `2732be4` |
|---|---|
| M-2 `db/queries.py` split | 1,316 lines, `init_db` CC 47 with 12 inline `CREATE INDEX` steps |
| M-9 ingestion router split | `api/routers/ingestion.py` 658 lines |
| M-6 layering contract | 191 function-level `pka` imports, 43 of them in `fetcher.py` |
| P-5 FTS5 index | no FTS5 table; `search_hits.py:94` still `title.ilike('%q%')` with no `LIMIT` |

## 3. Maintainability findings

Ordered by expected payoff. "Effort" is a rough S/M/L.

### M-14: `_fetch_one_impl` is a 212-line dispatch chain, CC 51 (M)

Evidence: `pka/ingestion/fetcher.py:338-549`. Cyclomatic complexity **51 (F)**,
the highest in the tree; the function holds **19 predicate rungs** and **16
function-level imports**, each rung the same shape — import the handler module,
test a URL predicate, return its result:

```
    from pka.ingestion.arxiv import fetch_arxiv_paper, parse_arxiv_url
    if parse_arxiv_url(url):
        return await fetch_arxiv_paper(...)
```

`fetcher.py` carries **24 commits** of churn, sixth-highest in the repo, and
pylint's duplicate-code report still lands entirely in this family:
`arxiv.py:177-210` ≡ `biorxiv.py:139-172`, `arxiv.py:241-260` ≡
`biorxiv.py:209-228`, `biorxiv.py:107-127` ≡ `reddit_bookmark.py:161-181`,
`arxiv.py:151-166` ≡ `biorxiv.py:107-122`, `pubmed.py:131-146` ≡
`reddit_bookmark.py:161-176`, `runners/firefox.py:126-148` ≡
`runners/reddit.py:185-207`.

**Why this is a new item and not M-4 again.** The previous audit measured CC 26
here and declined to file, on the grounds that
`planning/archive/FETCH_DISPATCH_PLAN.md` and
`planning/archive/PUBLISHER_FETCH_HANDLERS.md` already proposed the fix. Both are
now archived as implemented — and neither touched this function. The first
reordered *worker dispatch* so a throttled domain stops blocking a worker slot;
the second *added* doi.org, Nature, Springer, APS, ScienceDirect, MIT Press and
ResearchGate handlers. Shipping the second is what doubled the complexity: every
handler is another rung. Deferring again would defer to plans that are finished.

Recommendation: a handler table, in the shape `pka/ingestion/registry.py` already
uses for `PHASE_SPECS` — a frozen dataclass plus a module-level tuple:

```python
@dataclass(frozen=True)
class FetchHandler:
    matches: Callable[[str], bool]
    fetch: Callable[..., Awaitable[FetchResult] | FetchResult]
    awaits: bool = True          # search_url / researchgate / youtube_page are sync
```

`_fetch_one_impl` becomes a loop over that tuple plus the generic tail. Order is
load-bearing and must be preserved verbatim — the comment at `fetcher.py:417`
records that the publisher block has to stay after arXiv, because
`doi.org/10.48550/arXiv.…` is a valid arXiv DOI. Keep the imports lazy (they are
what stops `fetcher` pulling every handler at module scope); build the table on
first use rather than at import. The duplicated blocks above are separate work,
absorbed by a shared `fetch_base` template, and worth doing second.

### M-15: the test suite sleeps 41 seconds on the rate limiter (S, highest ratio)

Evidence, and this one is measured rather than argued. `AsyncRateLimiter` /
`SyncRateLimiter` (`pka/ingestion/rate_limit.py:87,109`) are instantiated at
module scope in three places — `fetch_base.py:58`, `openlibrary.py:78`,
`book_search.py:46` — and their `SlotScheduler` state is process-global, so it
persists across tests. Nothing in `tests/conftest.py` neutralises it. The result
is that a fetch-handler test waits a real second whenever it is the second call
against a domain, including when the first call was *a previous test*.

Twelve fetch-family test files, 328 tests:

| Run | Wall time |
|---|---|
| As-is | **33.8 s** |
| With `SlotScheduler.claim` returning 0 | **2.8 s** |

Across the whole runnable subset (1,568 tests): **82.2 s → 41.0 s**. Half the
suite's wall clock is `asyncio.sleep` and `time.sleep`.

The single-test case shows the same thing at small scale:
`test_wayback.py::TestFetchViaWayback::test_fetches_html_snapshot` takes 0.78 s
alone (it fetches twice against one host) and 1.82 s as part of its file, where
the earlier tests have already claimed the slot.

Three modules already work around this by hand, each patching a private name —
`tests/test_openlibrary.py:16`, `tests/test_book_search.py:15-16`,
`tests/test_wikipedia.py:167,190,245` all monkeypatch `_limiter.wait`. That is
the same private-name coupling M-11 spent effort removing elsewhere.

Recommendation: an autouse fixture in `tests/conftest.py` that sets the gap to
zero, alongside the existing Ollama/Chroma/HTTP/CLIP mocks it already owns —
patch `SlotScheduler.claim`, not the three `_limiter` instances, so a fourth
limiter added later is covered by construction. `tests/test_rate_limiter.py` is
the one module that must keep real timing; it opts out with a marker or a
module-scoped override. Confirmed pass condition: with the fixture in place,
exactly the 8 tests in `tests/test_rate_limiter.py` change behaviour
(`TestSlotScheduler` ×3, `TestNextSlot` ×2, `TestSyncRateLimiter` ×1, and the two
module-level spacing tests) and every other test still passes — that is what the
41 s run above showed.

### M-16: the mypy ratchet still freezes 20 modules and 76 errors (M, ongoing)

Evidence: `pyproject.toml`'s `[[tool.mypy.overrides]]` lists **20** modules with
`ignore_errors = true`. `mypy pka` is clean under the project config, and
**76 errors in 19 files** with the overrides removed — down from 89 in 22 when
M-7 shipped the ratchet, so roughly 13 errors have been retired in a week of
work that was not aimed at them.

This is the mechanism working as designed: M-7 chose "no new errors" over "fix 89
first", explicitly. The finding is that nothing schedules the drawdown, so the
list is load-bearing indefinitely and a module on it silently loses type checking
for unrelated future edits.

Recommendation: no refactor. Add the list to whatever the next few code-touching
items are — a module already being edited for another reason is the cheap moment
to take it off the list, which is how `pka.clustering.engine` came off during
M-1. Worth stating a target in `TODO.md` (say, list emptied by v0.1.0) so the
ratchet has a direction rather than only a floor.

### M-17: `tag_training/lifecycle.py` is the last B-grade module outside the excluded set (S/M)

Evidence: maintainability index **16.53**, the lowest in the tree other than
`db/queries.py` (15.41, excluded as M-2). It is the module the shared ingest tail
reaches on every document — `apply_learned_tags_for_document`
(`pka/tag_training/lifecycle.py:371`) is called from
`clustering/doc_embeddings.py` — so it sits on a hot path as well as a complex
one.

Recommendation: the seam is the same split that worked for `engine.py`. The file
mixes session lifecycle (create, accept, archive), label writes, and the scoring
path that ingestion calls. Extracting the scoring half —
`_apply_model_to_documents`, `apply_learned_tags_for_document`,
`_set_learned_overlay`, `_clear_learned_overlay` — into `tag_training/scoring.py`
gives ingestion a small module to depend on and leaves lifecycle to the API. It
also narrows the `ingestion → tag_training` edge that M-6 wants to break, so
sequence it before or with that work rather than against it.

### M-18: small hygiene, batchable (S)

- **Dead module.** `pka/api/schemas/common.py` defines `Pagination` and has
  **zero importers** anywhere in `pka/` or `tests/`. vulture misses it because a
  pydantic model reads as used. Delete it, or wire it into the list responses
  that currently hand-roll `total`/`limit`/`offset`.
- **40 `PLR0913` (too many arguments).** Most are legitimate FastAPI query
  signatures; a handful in `ingestion/` are the dataclass-boundary smell M-1
  fixed for clustering, and are worth the same treatment when those functions are
  next edited.
- **14 `ARG001` unused function arguments** — each is either a stale parameter or
  a protocol conformance the code should say so about.
- **70 `TRY003`** (long messages in `raise`) is noise at this scale and should be
  left alone; recording it so the next audit does not re-derive that judgement.

## 4. Performance findings

### P-9: `cluster_assignments` has no index for a cluster-scoped read (S, highest ratio)

Evidence: `pka/db/schema.py:229` declares exactly one index on the table,
`ix_cluster_assignments_run_id_document_id` on `(run_id, document_id)`, added by
P-1. Two query shapes filter on `(cluster_id, run_id)` instead:

- `_cluster_doc_count` (`pka/api/routers/clusters.py:49`)
- `cluster_document_ids` (`pka/clustering/cluster_tags.py:30`)

SQLite can use the existing index for the `run_id` half — it is the leading
column — and must then filter every row of that run to find the cluster's. The
table holds one row per document per run; run #2 in the dev notes carried 15,430
assignments. So a single cluster read touches the whole run.

The cost is worst where it loops. `apply_all_tags` (`clusters.py:159-181`)
iterates every cluster in the active run and calls `_apply_cluster_label`, which
calls `cluster_document_ids` once per cluster (`clusters.py:91`). For a run with
40 clusters over 15 k assignments that is ~600 k row reads to write ~15 k overlay
tags. `relabel_single_cluster` and the `/clusters/{id}` detail route pay the same
cost once each.

Note what is *not* wrong: `list_clusters` (`clusters.py:140`) already uses
`_cluster_counts`, a single `GROUP BY` over the run (`clusters.py:97-106`), so
the list endpoint is batched. The N+1 is confined to the apply path.

Recommendation: add `sa.Index("ix_cluster_assignments_run_id_cluster_id",
"run_id", "cluster_id")` to `pka/db/schema.py` **and** a matching
`CREATE INDEX IF NOT EXISTS` in `init_db` — `create_all` does not add indexes to
an existing table, which is why P-1 needed both. Checked before proposing:
`git log -S "ix_cluster_assignments"` shows only `fb99f30` (P-1's
`(run_id, document_id)`), so this has not shipped in some other form.

Measurement that would confirm it: `EXPLAIN QUERY PLAN` on
`SELECT document_id FROM cluster_assignments WHERE cluster_id = ? AND run_id = ?`
against the production archive, read-only. Today it should report `SEARCH
cluster_assignments USING INDEX ix_cluster_assignments_run_id_document_id
(run_id=?)`; with the new index it should name
`ix_cluster_assignments_run_id_cluster_id (run_id=? AND cluster_id=?)`. Timing
one `POST /clusters/apply-all-tags` before and after is the end-to-end version.

### P-10: things checked and found fine (no action)

Recording these so the next audit does not re-derive them, and correcting two
claims from the last one.

- **Startup cost is solved.** Cold `import pka.api.main` is 0.89 s. After it,
  `sys.modules` holds none of sklearn, chromadb, torch, umap, hdbscan or
  transformers — only numpy, which arrives through the schema layer and is cheap.
  P-2 is done and has not regressed.
- **The shared ingest tail no longer round-trips.** `upsert_chunks`
  (`storage/vector_store.py:200`) returns the vectors it computed and
  `refresh_document_embedding` (`clustering/doc_embeddings.py`) accepts them as
  `known=`, so a single-block document does zero Chroma reads; multi-block
  sources defer with `refresh=False` and refresh once. P-4 is done.
- **Over-fetch is gone from the hot paths.** No `sa.select(documents)` remains
  anywhere in `pka/`; the filter step and `documents_out_batch` name their
  columns. P-5's projection half is done.
- **The SSE probe cache still stands.** `_cached_probe`
  (`ingestion/pending_metadata.py:34`) memoises per `(kind, source)` for
  `ingestion_probe_cache_ttl_seconds`, invalidated at job start, finish and
  purge. P-6 stays withdrawn; do not re-derive it.
- **Correction to the last run's P-8.** It noted that `_workers`
  (`api/routers/ingestion.py:550`) "never prunes finished threads". True but
  harmless: the dict is keyed by source, so it holds at most one `Thread` per
  source — six entries — and `_queue_job` replaces the entry rather than
  appending. There is no unbounded growth. Not a finding.
- **Sync `def` handlers remain correct** for this sync-SQLAlchemy stack; the
  `async def` endpoints do in-memory work or hand off to `run_in_threadpool`, and
  `sync_source` is a plain `def` precisely because `_queue_job` may block joining
  a cancelled worker (the comment at `api/routers/ingestion.py:591`).

## 5. Test-suite health

**Runtime.** 1,568 tests pass in **82.2 s** in this container. Half of that is
the rate-limiter sleep in M-15; with it removed the same tests take 41.0 s. After
that fix the slowest remaining tests are
`test_clustering.py::TestRunClustering::test_returns_cluster_run_result` (4.48 s)
and `test_api_runs.py::TestRuns::test_trigger_run_queued` (4.40 s), both of which
drive a full `run_clustering` with UMAP and HDBSCAN mocked — the residual cost is
real PCA over synthetic data plus the sklearn import, and both are worth a
`--durations=0` look only after M-15.

**Structure.** The M-11 split has landed: the old 2,446-line `test_api.py` is ten
per-router modules, largest 532 lines, sharing the `client` fixture from
`conftest.py` and row builders from `tests/api_seed.py`. Test code is 19,494 SLOC
across 84 files, still roughly 1:1 with the backend.

**Coverage: not measurable in this environment, and the number below is not the
project's coverage.** A run over the executable subset reports 83.33 % against
the 85 % gate, but it excludes the seven modules listed in §1 — and those
exclusions are precisely the low-coverage files (`ingestion/fetcher.py` 39 %,
`image_pipeline.py` 38 %, `image_extractor.py` 28 %, `book_extractor.py` 26 %,
`runners/reddit.py` 19 %). Excluding a test file does not exclude the module it
covers, so the figure is depressed by construction. Run
`pytest --cov=pka --cov-report=term-missing` on a machine with `torch` and a real
`chromadb` before drawing any conclusion about the gate.

One coverage signal does survive the exclusions: `pka/api/schemas/common.py` at
**0 %** is not a testing gap but dead code, filed as M-18.

**Frontend.** 77 tests in 9 files, up from 51, now including the first store and
component tests. `npm run lint` and `npm run build` are clean. Bundle is 138 KB
main plus a lazily loaded 168 KB `TrendsView`; unchanged and fine.

## 6. Prioritised plan

Quick wins (an afternoon each, no design change):

1. **M-15** rate-limiter fixture in `conftest.py`. Halves suite wall time; the
   measurement is already done, so this is implementation only.
2. **P-9** `(run_id, cluster_id)` index plus the `init_db` line.
3. **M-18** hygiene batch: delete `schemas/common.py`, triage the `ARG001` list.

Medium (a focused day or two):

4. **M-14** fetch-handler table in `fetcher.py`, order preserved, imports still
   lazy. Then the duplicated publisher blocks into a shared template.
5. **M-17** split `tag_training/lifecycle.py`, scoring out from lifecycle.

Ongoing, not a discrete task:

6. **M-16** draw down the mypy override list opportunistically, and give it a
   target release in `TODO.md`.

## 7. Out of scope

Security was not reviewed; a separate `security-review` skill exists. No
benchmark was run against a real archive, so P-9 is ranked by code shape and by
what the schema forces SQLite to do, with nothing timed — its `EXPLAIN QUERY
PLAN` check is named above and is read-only. M-2, M-9, M-6 and P-5's FTS5 index
were excluded by agreement, as §2 records; if the maintainer's local work differs
from what this tree shows, those four need a re-read, not a re-audit.

## References consulted

- [Codacy: Cyclomatic complexity guide](https://blog.codacy.com/cyclomatic-complexity) and [Code quality metrics](https://blog.codacy.com/code-quality-metrics): CC < 10 median, > 15 review; maintainability index as a refactor flag.
- [Sonar: Cyclomatic complexity](https://www.sonarsource.com/resources/library/cyclomatic-complexity/).
- [CodeAnt: Seven axes of code quality](https://www.codeant.ai/blogs/seven-axes-of-code-quality): churn × complexity as the refactor signal; duplication < 5–10 %.
- [SQLite query planner](https://www.sqlite.org/optoverview.html) — leading-column rule for composite indexes, which is what P-9 turns on.
- [pytest: monkeypatch and fixtures](https://docs.pytest.org/en/stable/how-to/monkeypatch.html) — autouse fixture scoping for M-15.
- [typescript-eslint: no-explicit-any](https://typescript-eslint.io/rules/no-explicit-any/) — the rule M-10 adopted, checked here as clean.
