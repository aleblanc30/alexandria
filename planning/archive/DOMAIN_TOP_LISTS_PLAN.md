# Top domains & top rejected domains

Plan for the `planning/TODO.md` item under *Source connectors*:

> **top domains and top rejected domains** — display top 10 domains in the
> database and top 10 unfetchable domains to see where specific handlers are
> needed.

## 1. What already exists

The counting half is done — it shipped with the **Domain frequency report**
item (already ticked):

| Piece | Location |
|-------|----------|
| `extract_domain(url_or_path)` — http(s) host, lowercased, `www.` stripped | `pka/domains.py:15` |
| `domain_has_fetch_handler(domain)` — mirrors the dispatch chain in `_fetch_one_impl` | `pka/domains.py:34` |
| `build_domain_frequency_report(source=, limit=)` → rows of `{domain, count, has_handler, by_fetch_status}` | `pka/domains.py:50` |
| `alexandria domain-report [--source] [--limit] [--json]` | `pka/cli/domain_report.py` |
| Tests | `tests/test_domains.py` |

`by_fetch_status` already carries the per-domain `unfetchable` tally, so the
"rejected" ranking is a re-sort of data the existing scan produces — no second
query, no new column.

What is missing is everything above that function: no API endpoint, no frontend
surface. The report is CLI-only today, and the TODO item says *display*.

## 2. Design decisions

**Ranking key for "rejected" is `fetch_status = unfetchable` only.** `skipped`
is a deliberate policy outcome (non-HTML extension, Wikipedia special page,
non-HTML content-type), not a failure, so it must not inflate a domain's
apparent need for a handler. It stays visible in the row's `by_fetch_status`
breakdown, just not in the sort.

**Domains that already have a handler are shown, not filtered out.** A handler
domain sitting high on the rejected list means the *existing* handler is
failing — that is a signal worth seeing, not noise to hide. The row carries a
`has_handler` badge so the two cases are distinguishable at a glance.

**Placement: the global `/ingestion` page**, below the source grid — not
`/ingestion/:source`. Domains span sources (Firefox bookmarks, Zotero item
URLs), and the question the lists answer ("which handler do I write next?") is
archive-wide. The API keeps the `source` filter for CLI parity and for a later
per-source panel; the first UI cut does not pass it.

**No cache.** The scan is one `SELECT url_or_path, fetch_status FROM documents`
plus an `urlparse` per row, and the endpoint is hit once per `/ingestion` mount
— it is not on the 2x/sec progress-poll path that forced the `_probe_cache` TTL
in `pka/ingestion/pending_metadata.py`. If it ever becomes hot, that module
already has the pattern to copy.

**Ties break on domain name ascending**, so the list is stable across calls and
testable without ordering flake.

## 3. Backend

### 3.1 `pka/domains.py`

Add the derived `unfetchable` count to every row emitted by
`build_domain_frequency_report`, so both lists share one row shape and "how many
failed here" has a single definition:

```python
"unfetchable": status_by_domain[domain].get(str(FetchStatus.UNFETCHABLE), 0),
```

(imports `FetchStatus` from `pka.constants`; additive, so the existing
`by_fetch_status` assertions in `tests/test_domains.py` keep passing).

Then add the pairing function:

```python
def build_domain_top_lists(
    *,
    source: str | None = None,
    limit: int = 10,
) -> dict[str, list[dict[str, Any]]]:
    """Top domains by document count and by unfetchable count, from one scan."""
    rows = build_domain_frequency_report(source=source)  # unlimited, count-sorted
    rejected = sorted(
        (r for r in rows if r["unfetchable"] > 0),
        key=lambda r: (-r["unfetchable"], r["domain"]),
    )
    return {"top_domains": rows[:limit], "top_unfetchable": rejected[:limit]}
```

One DB pass feeds both lists. Domains with zero failures are dropped from the
rejected list rather than padding it to 10 with noise.

### 3.2 `pka/api/schemas/ingestion.py`

```python
class DomainRow(BaseModel):
    domain: str
    count: int
    unfetchable: int
    has_handler: bool
    by_fetch_status: dict[str, int]


class DomainTopLists(BaseModel):
    top_domains: list[DomainRow]
    top_unfetchable: list[DomainRow]
```

### 3.3 `pka/api/routers/ingestion.py`

Next to `GET /ingestion/unfetchable`, which is the sibling view (per-URL
failures vs. per-domain aggregate):

```python
@router.get("/domains", response_model=DomainTopLists)
def domain_top_lists(source: str | None = None, limit: int = 10):
    """Top domains by document count and by unfetchable count."""
    if source:
        require_source(source)          # 400 on an unknown source
    if not 1 <= limit <= 100:
        raise HTTPException(400, "limit must be between 1 and 100")
    return build_domain_top_lists(source=source, limit=limit)
```

Declared `def`, not `async def`, deliberately: the full-table scan is blocking
DB work and FastAPI runs sync endpoints in its threadpool. (The neighbouring
`async def unfetchable_urls` blocks the event loop for its query — a
pre-existing wart, out of scope here; do not "fix" it in this change.)

## 4. Frontend

### 4.1 `frontend/src/api/client.ts`

Types beside `UnfetchableRow`, function beside `unfetchableUrls`:

```ts
export interface DomainRow {
  domain: string
  count: number
  unfetchable: number
  has_handler: boolean
  by_fetch_status: Record<string, number>
}
export interface DomainTopLists { top_domains: DomainRow[]; top_unfetchable: DomainRow[] }

export const domainTopLists = (limit = 10, source?: string) =>
  req<DomainTopLists>(
    `/ingestion/domains?limit=${limit}${source ? `&source=${encodeURIComponent(source)}` : ''}`,
    {},
    INGESTION_TIMEOUT_MS,
  )
```

### 4.2 `frontend/src/stores/ingestion.ts`

Add `const domains = shallowRef<api.DomainTopLists | null>(null)`, hydrate it in
`load()` with the same non-critical `try/catch` the `unfetchable` fetch uses
(lines 178–180), and export it. It is load-once state — deliberately *not* part
of the SSE `SyncEvent` frame, which stays lean for the running-sync path.

### 4.3 `frontend/src/components/DomainTopLists.vue` (new)

Two ranked tables in a 2-column grid that collapses to 1 below ~700px, each
built from the existing `.section-title` + `.table-wrap` classes in
`frontend/src/styles/global.css` — same visual language as the unfetchable table
in `IngestionSourceView.vue`.

- **Top domains** — rank, domain, `{{ count }} docs`, `handler` chip when
  `has_handler`.
- **Top unfetchable domains** — rank, domain, `{{ unfetchable }} / {{ count }}
  failed`, same chip.

Empty states: *"No HTTP(S) URLs ingested yet."* and *"No fetch failures
recorded."* Both lists are empty on a fresh archive, so this is the first thing
a new user sees — it must not render as a broken box.

Props: `{ data: DomainTopLists | null }`; the component is presentational and
does no fetching, so it stays trivially reusable if a per-source panel wants it
later.

### 4.4 `frontend/src/views/IngestionView.vue`

Render `<DomainTopLists :data="ingest.domains" />` after the `.source-grid` and
before the experimental-sources toggle. `onMounted(() => ingest.load())` already
runs — no new lifecycle wiring.

## 5. CLI parity (small, include it)

`alexandria domain-report --rejected` — sort by unfetchable count instead of
document count, so the terminal answers the same question as the UI. In
`pka/cli/domain_report.py`: one `add_argument`, and branch to
`build_domain_top_lists(...)["top_unfetchable"]` when set. `_print_table`
already prints the status breakdown, so no formatting work.

## 6. Tests

**`tests/test_domains.py`** — new `TestBuildDomainTopLists`:

- both lists come back from one seeded archive, ranked as expected;
- a domain with zero `unfetchable` is absent from `top_unfetchable`;
- equal unfetchable counts tie-break on domain name ascending;
- `limit` truncates both lists independently;
- empty archive → `{"top_domains": [], "top_unfetchable": []}`;
- plus one assertion that `build_domain_frequency_report` rows now carry
  `unfetchable`.

Seed with `tests.conftest.make_document(..., fetch_status=FetchStatus.X)` —
already used by the existing class in that file.

**`tests/test_api.py`** — in the ingestion class beside
`test_unfetchable_returns_list`:

- `GET /ingestion/domains` → 200 with both keys;
- `?limit=0` and `?limit=101` → 400;
- `?source=nope` → 400;
- seeded ranking round-trips through the response model.

**`frontend/src/api/client.test.ts`** — URL construction for `domainTopLists`
with and without `source`, following the existing `search` mock-`fetch` pattern.
There is no component-test harness in this repo (only `lib/` and `api/` specs),
so `DomainTopLists.vue` gets no unit test.

Verify with `pytest`, then `cd frontend && npm run test && npm run build`.

## 7. Docs

- `README.md:132` — add the `--rejected` line to the `domain-report` block.
- `planning/TODO.md` — tick the item and point it at this file, the way the
  other worked-out items point at theirs.
- **`docs/ingestion-flows.md` needs no change.** This is a read-only report over
  rows the pipelines already wrote: no phase shape, no shared/source-specific
  boundary move, no outbound call, no change to the shared tail. Confirmed
  against the sync-rule checklist in `CLAUDE.md`.
- `DESIGN.md` needs no change — it does not enumerate API endpoints.

## 8. Risks & follow-ups

- **`domain_has_fetch_handler` is a hand-maintained duplicate** of the dispatch
  chain in `_fetch_one_impl` (`pka/ingestion/fetcher.py:126`). It is already
  slightly looser than the real thing (`is_amazon_host` vs. the dispatch's
  `is_amazon_book_url`) — correct for a domain-level report, but it means every
  new handler from the TODO list (YouTube, Reddit, PubMed, more Amazon TLDs)
  must update `pka/domains.py` too, or the badge silently lies. Worth a comment
  on the function pointing at the dispatch site; a shared predicate registry is
  a separate refactor, not this change.
- **The lists are a snapshot at page load.** They do not refresh while a sync
  runs, so counts go stale during a long Firefox fetch. Acceptable for a
  prioritisation aid; revisit only if it actually annoys.
- **`build_domain_frequency_report` scans every document row**, including
  Zotero/Calibre rows whose `url_or_path` is a local path and which
  `extract_domain` immediately discards. Fine at current archive sizes; a
  `WHERE url_or_path LIKE 'http%'` prefilter is the cheap fix if it ever shows
  up in a profile.
