# M-3: split the `/search` route, and the P-5 column projections that ride with it

Plan for `planning/MAINTAINABILITY_PERFORMANCE_AUDIT.md` §M-3, plus the two
`select(documents)` projections from §P-5 that touch the same code path. FTS5
stays out of scope and keeps its own follow-up, as the audit's §6 ordering
already assumes.

## Why now

`pka/api/routers/search.py::search` is 160 lines in one function body: radon CC
73 (F), ruff C901 28, 27 branches, 75 statements, 13 commits. It runs five
stages back to back, each of which is independently meaningful and none of which
can be exercised without going through FastAPI's test client. `TestSearch` in
`tests/test_api.py` is 275 lines and 18 tests, every one of them a `POST /search`
round trip, because that is currently the only way in.

The route is also where several filter families converge, so every new filter
lands in the same body. Two of the audit's performance items (P-5's unbounded
`select(documents)` calls) sit inside it, and one of them scales with the
pre-pagination result count rather than the page size.

## What the function does today

| Lines | Stage | Produces |
|---|---|---|
| 35-58 | Semantic / hybrid Chroma query, with a logged fallback when the vector store is unavailable | `results` sorted by similarity, descending |
| 64-77 | Fulltext `title ILIKE` scan, run when the mode asks for it **or** the semantic stage returned nothing | ids not already present, appended in `documents.id` order, similarity `None` |
| 87-119 | CLIP cross-modal merge, gated on `req.query.strip()` and images being in scope | scored entries re-sorted descending, then unscored in prior order |
| 122-140 | Browse-style filters via `filter_document_ids` (sources, source/general/cluster tags, wayback) | same list, order preserved, non-matching dropped |
| 143-182 | Row filters: `fetch_status`, `date_from` / `date_to`, `cluster_ids` | same list, order preserved |
| 185-188 | `total = len(results)`, slice the page, serialize | `SearchResponse` |

Every stage consumes and returns `list[tuple[int, float | None]]`. That shared
shape is what makes the split cheap, and it is already the input type
`documents_out_batch` expects.

## Target layout

A new `pka/api/search_hits.py`, a peer of the existing `pka/api/image_hits.py`
and `pka/api/document_serialize.py`. Those two set the precedent: non-router
logic that a router imports, unit tested on its own terms. The router keeps the
HTTP surface and the stage ordering, and nothing else.

| New symbol in `pka/api/search_hits.py` | Moves from | Signature |
|---|---|---|
| `Hits` (type alias) | new | `list[tuple[int, float \| None]]` |
| `semantic_hits` | `search.py:35-58` | `(req: SearchRequest) -> Hits` |
| `fulltext_hits` | `search.py:64-77` | `(con, req: SearchRequest) -> Hits` |
| `merge_new` | `search.py:74-77` | `(base: Hits, extra: Hits) -> Hits` |
| `merge_clip_hits` | `search.py:87-119` | `(con_free) (results: Hits, req: SearchRequest) -> Hits` |
| `apply_browse_filters` | `search.py:122-140` | `(con, results: Hits, req: SearchRequest) -> Hits` |
| `apply_row_filters` | `search.py:143-182` | `(con, results: Hits, req: SearchRequest, run_id: int \| None) -> Hits` |

`search.py` then reads as the stage sequence plus pagination, roughly 35 lines
of body. The file docstring's note about N+1 avoidance stays where it is, since
`documents_out_batch` is still called from the router.

Two deviations from the audit's sketch, both deliberate:

1. The audit proposed `_fulltext_hits(con, req, existing)`. Splitting that into
   `fulltext_hits(con, req)` plus a separate `merge_new(base, extra)` lets the
   query be tested without first constructing a prior result list, and puts the
   dedup policy in one named place instead of inside a loop. The SQL is
   unchanged. Both versions materialise the full match list, so there is no
   memory regression.
2. The helpers are public names in a new module rather than underscore-prefixed
   functions in the router. Tests importing `pka.api.search_hits` do not reach
   into a router's privates, and a later MCP path (`planning/MCP_PLAN.md` §"No
   second copy of the query logic") has something importable if it ever needs
   the stages without HTTP.

## Ordering invariants the split must preserve

These are the parts a careless extraction breaks silently, because every one of
them still returns a plausible-looking result list. Each gets a unit test.

- The fulltext stage runs when `req.mode in ("fulltext", "hybrid")` **or**
  `results` is empty. The second half is the vector-store fallback and is what
  `test_semantic_query_failure_falls_back_to_fulltext` covers today. It is easy
  to lose when the condition moves into a function.
- Fulltext entries carry similarity `None`, and `None` is not the same as `0.0`
  anywhere downstream: the CLIP merge partitions on `is not None`, and
  `DocumentOut.similarity` renders it.
- The CLIP merge re-sorts. In `fulltext` mode with CLIP hits present, documents
  that arrived in `documents.id` order come back with scored ones first. That is
  intended behaviour (the comment at `search.py:80-86` explains why), and it must
  survive. Unscored entries keep their prior relative order via dict insertion
  order.
- `cluster_membership` is populated only when `req.cluster_ids and run_id`. With
  `cluster_ids` set and no active run, every document is dropped, because
  `cluster_membership.get(doc_id)` yields `None` and `None not in req.cluster_ids`.
  Preserve it exactly; a test should pin it so a future reader does not "fix" it
  into a no-op filter.
- `total` is computed after all filtering and before slicing, so it counts
  matches rather than returned rows.
- The browse-filter stage is skipped entirely when `results` is empty, which
  matters because `filter_document_ids` returns `set()` for an empty id list
  either way. The guard is cheap and should move with the code.

## P-5, the two column projections

Both are `select(documents)` over every column, including the 1.5 KB
`doc_embedding` blob and `generated_summary`.

**`search.py:149`, the row-filter fetch.** This one is sized by the
pre-pagination result count, not by `limit`. A fulltext query matching several
thousand titles with a date filter pulls the blob for every one of them to read
three columns. Project to `id`, `fetch_status`, `date_added`. This is the larger
of the two wins and it lands naturally inside `apply_row_filters`.

**`document_serialize.py:65`, the card fetch.** Sized by the page, so 20 rows by
default. It reads 16 columns and can drop 7: `ingested_at`, `item_type`,
`doc_embedding`, `generated_summary`, `summary_run_id`, `zotero_url`,
`zotero_path`.

The risk here is specific and worth naming. `documents_out_batch` reads optional
fields with `row.get("archive_url")` on a `Mapping`, so a column left out of the
projection returns `None` rather than raising. The failure mode is a silently
blank field in the API response, not an exception. Mitigation: declare the
projected columns as a module-level tuple next to the builder, and add a test
that seeds one document with every column populated and asserts each
`DocumentOut` field comes back non-`None`. That turns the silent `None` into a
test failure. The same test guards future column additions.

`document_detail` (`document_serialize.py:239`) also does `select(documents)`,
on a single row. The audit notes it "sets the pattern" but concedes it is fine.
Leave it: it reads a different column set (`item_type`, via `_reddit_detail`),
and changing it adds the same silent-`None` risk for no measurable gain.

## Out of scope

- **FTS5.** The audit and `BACKLOG.md` both want an FTS5 virtual table over
  `title` + `card_summary`, kept in sync by the `DocumentWrite` path, replacing
  the unbounded `ILIKE`. It is a schema change with a migration and a backfill,
  and it deserves its own plan file. What this split buys it is a single
  `fulltext_hits` function to swap the query inside.
- **The semantic over-fetch ceiling.** `n_results=(req.offset + req.limit) * 3`
  grows linearly with page depth. Capping it is one line inside `semantic_hits`,
  but it changes results at deep pagination, so it does not belong in a commit
  whose acceptance criterion is "no behaviour change". Sequenced separately
  below.
- **Shrinking `TestSearch`.** The audit says it "would then shrink to
  endpoint-shape checks". Keep all 18 tests. They are the only regression
  harness the refactor has, and CLAUDE.md's rule against deleting tests applies
  with full force to the tests that prove a refactor was behaviour-preserving.
  Revisit only after the unit tests exist and are demonstrably covering the same
  ground.

## Commit sequence

1. **Extract the stages.** Create `search_hits.py`, move the five stages,
   rewrite the router body. No SQL changes, no behaviour changes. `TestSearch`
   must pass untouched; that is the whole point of doing this first and alone.
2. **Add `tests/test_search_hits.py`.** Unit tests per stage against a real
   `tmp_path` SQLite connection (`tests/conftest.py` already redirects data paths
   and mocks Chroma), covering the invariants listed above.
3. **Project the row-filter columns** (`apply_row_filters`).
4. **Project the card columns** (`documents_out_batch`), with the
   all-fields-populated test in the same commit.
5. **Cap the semantic over-fetch** as a named constant, with a test at a deep
   offset asserting the cap applies. Separate commit, separate line in the
   commit message, because it is the only behaviour change in the batch.

Commits 3 and 4 are independent of each other; either can be dropped without
affecting the rest.

## Verification

`scripts/check.sh` for the whole gate. During the work, `pytest tests/test_api.py
tests/test_search_hits.py` is the fast loop.

Two configuration facts bear on this specific change:

- Both `pka.api.routers.search` and `pka.api.document_serialize` are on the mypy
  `ignore_errors` override list in `pyproject.toml`. `search_hits.py` starts off
  that list and must typecheck clean from its first commit. Once the router body
  is 35 lines, check whether `pka.api.routers.search` can come off the list too,
  the way `pka.clustering.engine` did in M-1. Do not add the new module to the
  list to make a deadline.
- Coverage runs with `fail_under = 85`, so run `pytest --cov=pka` before the
  final commit rather than discovering the dip afterwards.

Frontend is untouched: `SearchResponse` and `DocumentOut` keep their shapes, so
no `npm run build` implications beyond the standard check-script run.

## Documentation

Neither `docs/ingestion-flows.md` nor `docs/persisted-fields.md` needs an update,
and it is worth stating why so the next reader does not go looking. The flow
graphs draw ingestion pipelines, and no pipeline changes here. `persisted-fields`
records what each source *writes*; this is a read path only, and the column
projections change which columns are `SELECT`ed, not which are stored.

The audit file itself should get M-3 and the two P-5 bullets marked as addressed
once this ships, and `planning/TODO.md` gets the usual one-line record of what
actually landed.
