# Search-result URLs → a card built from the query, with no fetch

A bookmarked search-results page (`google.com/search?q=dark+matter+halos`,
`duckduckgo.com/?q=…`, `youtube.com/results?search_query=…`) is not a document.
Today it goes down the generic path in `_fetch_one_impl`
(`pka/ingestion/fetcher.py:172`): an httpx GET, a rate-limiter slot, then
`trafilatura` against a JS-rendered SERP. The three possible outcomes are all
bad — a bot-check page scraped as if it were content, `HTTP 429`/`403` →
`unfetchable`, or a wall of "People also ask" boilerplate embedded as the
document's text and excerpted onto its browse card.

But the interesting part of that bookmark is already *in the URL*: the query
string. This plan adds a fetch-pool handler that recognizes a search URL,
decodes the query, and returns a finished card — **without making any HTTP
request at all**.

## 1. Where it goes: a no-network handler in the fetch pool

`pka/ingestion/search_url.py` (new), dispatched from `_fetch_one_impl` exactly
like `arxiv` / `pubmed` / `reddit_bookmark`, following the shared handler shape
recorded in `FIREFOX_INGESTERS_PLAN.md` §1:

| Piece | Here |
|---|---|
| `is_search_engine_host(url)` | Host-only check, for `domains.py` (§6) |
| `parse_search_url(url) -> SearchQuery \| None` | Decode engine + query, or `None` |
| `search_url_result(doc_id, url) -> FetchResult \| None` | Build the card; **not `async`, takes no `client`** |
| Dispatch in `_fetch_one_impl` | One `if` block, placed first (§3) |
| `domains.py::domain_has_fetch_handler` | Mirror predicate |

**Why the fetch pool and not phase 1.** The obvious alternative is to catch
search URLs in `runners/firefox.py::_persist`, next to the existing
`bookmark_url_unfetchable_reason` branch, and write title + `card_summary`
straight away. Rejected: that path writes metadata only. The document would
never acquire chunks, so the card would exist but be invisible to search — and
`source_ingest_queue` (`pka/db/queries.py:534`) would keep re-queueing it
forever as a `fetched`-missing-chunks orphan. Phase 2 already owns the
title/`card_summary`/`fetch_log` write (`_persist_fetch_result`) *and* the
inline embed branch, so a handler there is ~50 lines and needs no new wiring.

The handler is synchronous and never touches `client` or `_limiter`. That is
the whole point, and it is what the tests should assert (§7).

## 2. Recognition: one data table, not a chain of `if`s

```python
@dataclass(frozen=True)
class SearchEngine:
    name: str            # "Google", "YouTube" — display name for the card
    host: re.Pattern     # matched against the parsed hostname
    paths: tuple[str, ...] | None   # exact paths; None = any path
    params: tuple[str, ...]         # query params to try, in order

@dataclass(frozen=True)
class SearchQuery:
    engine: str
    query: str
```

`parse_search_url` finds the first engine whose host matches and whose path is
in `paths` (or any path when `paths is None`), then walks `params` and takes
the first non-blank value from `parse_qs` — which already handles `+` and
percent-decoding. Whitespace is collapsed and the result truncated with
`truncate_summary` (`pka/card_summary.py`) so a pathological query cannot
become a 40 KB title. Blank query → `None` (falls through to the generic path,
which is the right answer for a bare `google.com/`).

### Tier 1 — general web engines (ship this)

| Engine | Host | Paths | Params |
|---|---|---|---|
| Google | `^(www\.)?google\.[a-z.]+$` | `/search` | `q` |
| Google Scholar | `^scholar\.google\.[a-z.]+$` | `/scholar` | `q` |
| Bing | `^(www\.)?bing\.com$` | `/search`, `/images/search` | `q` |
| DuckDuckGo | `^(html\.\|lite\.)?duckduckgo\.com$` | `/`, `/html`, `/lite` | `q` |
| Brave | `^search\.brave\.com$` | `/search`, `/images` | `q` |
| Ecosia | `^(www\.)?ecosia\.org$` | `/search`, `/images` | `q` |
| Startpage | `^(www\.)?startpage\.com$` | `/sp/search`, `/do/search` | `query`, `q` |
| Qwant | `^(www\.)?qwant\.com$` | `/` | `q` |
| Yandex | `^(www\.)?yandex\.(com\|ru)$` | `/search/` | `text` |
| Baidu | `^(www\.)?baidu\.com$` | `/s` | `wd`, `word` |

### Tier 2 — site-scoped searches (same table, add later or now)

| Engine | Host | Paths | Params |
|---|---|---|---|
| YouTube | reuse `youtube_bookmark._YOUTUBE_HOST` | `/results` | `search_query` |
| Reddit | reuse `reddit_bookmark._REDDIT_HOST` | `/search`, `/search/`, `^/r/[^/]+/search/?$` | `q` |
| Amazon | reuse `amazon.is_amazon_host` | `/s` | `k` |
| GitHub | `^(www\.)?github\.com$` | `/search` | `q` |
| Stack Overflow | `^(www\.)?stackoverflow\.com$` | `/search` | `q` |
| PubMed | reuse `pubmed._PUBMED_HOST` | `/` | `term` |
| Wikipedia | reuse `wikipedia._WIKI_HOST` | `/wiki/Special:Search`, `/w/index.php` | `search` |

Tier 2 is *data*, so splitting the rollout costs nothing but review surface.
The Reddit `/r/<sub>/search` row is the one entry needing a regex rather than
an exact path — either give `paths` an optional regex variant or normalize
`/r/<sub>/search` before the lookup; the former is cleaner.

**Non-goals.** Fragment-based legacy queries (`google.com/#q=…`) — the fragment
never reaches the server and rarely survives a bookmark. Guessing at unknown
hosts by sniffing for a `q=` param: too many false positives (`?q=` is a
pagination and filter param on plenty of sites).

## 3. Dispatch order: first, before Wikipedia

In `_fetch_one_impl`, immediately after the `bookmark_url_unfetchable_reason`
guard and **before** `is_wikipedia_special`:

```python
from pka.ingestion.search_url import search_url_result

if (result := search_url_result(doc_id, url)) is not None:
    return result
```

Going first matters:

- `wikipedia.is_wikipedia_special` currently turns `Special:Search?search=…`
  into `status="skipped"` with no card at all. A search card is strictly better.
- `youtube.com/results?search_query=`, `reddit.com/r/x/search?q=`,
  `pubmed.ncbi.nlm.nih.gov/?term=`, `amazon.com/s?k=` all happen to fall
  through their host handlers' `parse_*` today (they need a video ID, a
  `/comments/` permalink, a numeric PMID path, an ASIN respectively). Dispatching
  search first makes that independent of those parsers' internals rather than a
  coincidence that a future edit could break.

Note `search_url_result` is sync while every neighbour is `await`ed — keep the
call un-`await`ed and let the walrus read naturally.

## 4. What the card actually says

For `https://www.google.com/search?q=dark+matter+halos`:

- **`title`** = `dark matter halos — Google search`
  The query leads, because `fetched_embed_text(title, summary, text)` puts the
  title into the embedded blob and the query is the entire semantic payload.
  The `— Google search` suffix is what stops a browse list from showing a bare
  phrase indistinguishable from a real document title.
- **`card_summary`** = `Saved Google search for "dark matter halos".`
  Set explicitly, so `embed_fetched_text` does not fall back to
  `body_excerpt(text)` (`runners/firefox.py:104`).
- **`text`** = `dark matter halos\n\nGoogle search` — the embed body. Short by
  design; there is nothing else true to say.

`FetchResult` already carries `title` and `card_summary`
(`fetch_base.py:27`), so **no schema change and no new field**.

## 5. `fetch_status` stays `fetched`

Tempting to add a `FetchStatus.SEARCH` — don't. Two places key off `fetched`:

- `source_ingest_queue` re-queues only `pending` + `fetched`-missing-chunks. A
  novel status silently drops the document out of orphan backfill, so a failed
  embed would never be retried.
- `_run_fetch_workers`'s embed branch fires on `r.text` being set, which would
  still work — but the two would then disagree about what the row means.

The precedent already exists: `reddit_bookmark._url_fallback_result` returns
`status="fetched"` for a card built purely from the URL slug when the `.json`
listing is blocked. Follow it, and record the provenance the same way it does —
in the `fetch_log` row `_persist_fetch_result` writes:

```python
FetchResult(doc_id, url, "fetched", text, None,
            "search url; card built from query, no fetch",
            title=..., card_summary=...)
```

`http_status=None` is honest: there was no response.

## 6. Wiring

### `pka/domains.py`

`domain_has_fetch_handler` probes `f"https://{domain}/"` — a bare host with no
query. Every predicate in its `or` chain is host-level, so a *URL*-level search
predicate would never match and the domain report would show `google.com` as
unhandled. Add the host-only variant:

```python
from pka.ingestion.search_url import is_search_engine_host
...
    or is_search_engine_host(probe)
```

Accepted imprecision, worth a comment: this marks *every* `google.com` row as
handled, including `google.com/maps/…`. It is a report column, and search URLs
dominate those domains' rows in practice.

### `pka/config.py` (optional, recommended)

`search_url_cards: bool = True`, listed in the `_parse_bool` validator
(`config.py:431`) alongside `fetch_wayback_fallback` — the existing precedent
for a default-on behavioural toggle. **No `DESIGN.md` §1.1 flag is required**:
§1.1 governs *outbound* paths, and this handler removes requests rather than
adding one. A fresh checkout with no `.env` makes strictly fewer calls than
before.

### `docs/ingestion-flows.md` (mandatory, same commit — see CLAUDE.md)

- Firefox graph, `handlers` subgraph (line ~275): add
  `SRCH["search_url_result()<br/>query decoded from the URL — no request"]`
  and `DISPATCH --> SRCH`. **Deliberately draw no edge to `NET`** — it is the
  only node in that subgraph without one, and that absence is the feature.
- Reddit graph, line 541: the `POOL` label enumerates the dispatch chain
  (`wikipedia / youtube / reddit / …`); add `search` at the front.

### `docs/persisted-fields.md`

§"What each column means per source", the Firefox `card_summary` cell currently
reads `body_excerpt` of the fetched text — extend to
`` `body_excerpt` of the fetched text; `Saved <engine> search for "<query>"` for a search URL ``.
The `fetch_status` cell needs no change (`fetched`, per §5).

## 7. Tests — `tests/test_search_url.py`

The load-bearing one first:

1. **No HTTP.** Drive `_fetch_one_impl` with a mock `httpx.AsyncClient` whose
   `get` raises, and assert a Google search URL still returns a `fetched`
   result. This is the feature; everything else is detail.
2. **Parse table.** One positive per tier-1 engine (parametrized), asserting
   engine name and decoded query — including `+`, `%20`, and a non-ASCII query.
3. **Negatives.** `google.com/maps/place/…`, `google.com/` with no `q`,
   `?q=` blank, `?q=%20%20`, a YouTube watch URL, a Reddit `/comments/`
   permalink, a bare `duckduckgo.com`.
4. **Dispatch precedence.** `youtube.com/results?search_query=x` produces a
   search card, not a YouTube-oEmbed attempt; `…/wiki/Special:Search?search=x`
   produces a card rather than `status="skipped"`.
5. **Card shape.** `title`, `card_summary`, `text`, `http_status is None`, and
   the `error_msg` provenance string.
6. **Truncation.** A 5000-char `q` yields a title within `SUMMARY_MAX_LEN`.
7. `tests/test_domains.py` — `domain_has_fetch_handler("google.com")` is `True`.

`tests/conftest.py` already mocks HTTP, Ollama, Chroma and CLIP; nothing new is
needed there.

## 8. Existing rows (optional migration)

Bookmarks already ingested keep whatever they got. Two populations:

- `fetch_status='unfetchable'` search URLs — re-queue by setting them back to
  `pending`; the next fetch pass will produce cards. Harmless.
- `fetch_status='fetched'` search URLs carrying scraped SERP boilerplate — these
  also need their chunks and Chroma vectors dropped before re-queueing, or the
  garbage stays in the index alongside the new card. That is exactly what
  `PURGE_AND_PROVENANCE_PLAN.md` is for; do not hand-roll a second purge path.

`reset_unfetchable_for_fetch` is not the tool — it is dev-only and re-queues
*everything*. Treat this section as a follow-up, gated on the purge work; the
handler is useful on its own for everything ingested after it lands.

## 9. Suggested order

1. `pka/ingestion/search_url.py` with the tier-1 table + `tests/test_search_url.py`.
2. Dispatch block in `_fetch_one_impl`; the precedence tests go green.
3. `domains.py` predicate + its test.
4. Tier-2 rows (data only, extend the parametrized test).
5. `config.py` toggle, if wanted.
6. `docs/ingestion-flows.md` + `docs/persisted-fields.md` — **same commit**.
7. Tick the `planning/TODO.md` line.
