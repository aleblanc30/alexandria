# Additional Firefox fetch handlers: YouTube, Reddit, PubMed

Plan for the `planning/TODO.md` items under *Source connectors*:

> - **Youtube ingester** — Firefox fetch handler for youtube pages saved as bookmarks
> - **reddit ingester** — Firefox fetch handler for reddit pages saved as bookmarks
> - **amazon ingester** — extend current amazon ingester to other domains (.fr, .com, .in ...)
> - **pubmed ingester** — Firefox fetch handler for pubmed pages saved as bookmarks

These are **domain-specific enrichment handlers inside the Firefox bookmark
fetch pool** (`pka/ingestion/fetcher.py`), the same family as the existing
`arxiv.py` / `biorxiv.py` / `amazon.py` / `wikipedia.py` modules. They are
**not** the same thing as the `Source.YOUTUBE` / `Source.REDDIT` connectors
(`pka/connectors/youtube.py`, `pka/connectors/reddit.py`), which pull the
user's *own* saved/liked items via authenticated APIs. A Firefox bookmark that
happens to point at `youtube.com` or `reddit.com` is a link the user saved from
somewhere else — a different problem the two source-specific connectors don't
touch — and today it falls through to generic `trafilatura` scraping, which
does badly against both sites' JS-heavy markup.

## 0. The Amazon item is already done

`is_amazon_host()` (`pka/ingestion/amazon.py:34`) is
`^([a-z0-9-]+\.)*amazon\.[a-z.]+$` — already matches any TLD, confirmed against
`.com`, `.co.uk`, `.fr`, `.in`, `.de`, `smile.amazon.com`. The TODO item appears
to predate that regex's generalization (it shipped already-generic in
`7724cee`). Nothing to build here.

**Action:** add one regression test locking in a non-`.com`/`.co.uk` TLD (e.g.
`amazon.in` or `amazon.de`) to `TestAmazonHost` in `tests/test_amazon.py`, then
tick the TODO line and drop the "extend to other domains" wording — it no
longer describes outstanding work.

The rest of this plan covers the three genuinely new handlers.

## 1. Shared pattern (recap, not new)

Every existing handler follows the same shape and this plan reuses it exactly:

| Piece | Purpose |
|---|---|
| `is_<source>_url(url)` | Cheap host check |
| `parse_<source>_url(url)` | Extract the stable ID (arXiv ID, DOI, ASIN) or `None` |
| `fetch_<source>_*(client, doc_id, url)` | Own HTTP call(s) via `_limiter.wait()` + `_http_timeout()`, returns `FetchResult \| None` (`None` only when the URL didn't actually match) |
| Dispatch in `_fetch_one_impl` | One `if parse_x(url): result = await fetch_x(...); if result is not None: return result` block |
| `pka/domains.py::domain_has_fetch_handler` | Mirror predicate, or the domain report silently under-counts |

`FetchResult` (`pka/ingestion/fetch_base.py:27`) already carries every field
these three handlers need: `title`, `card_summary`, `doi`, `year`,
`authors_json`. No schema change.

**No `_fetch_budget_seconds` change needed.** That function adds extra budget
for wikipedia (retries) and `preprint` (arxiv/biorxiv: metadata call *and* a
PDF call). All three new handlers make exactly **one** HTTP request, same as
the generic HTML path — they fit the existing base budget.

**No new config settings.** Per `DESIGN.md` §1.1, Firefox phase-2 fetch is
already in the "reads back... a URL you bookmarked" bucket that needs no named
flag — these are more URLs going through the same always-on fetch path, not a
new outbound capability class.

## 2. YouTube — `pka/ingestion/youtube_bookmark.py` (new)

Deliberately **not** named `youtube.py` — that name is free, but the module
sits next to `pka/ingestion/youtube_sync.py` / `runners/youtube.py` /
`connectors/youtube.py`, which are the unrelated Data-API connector. Call it
`youtube_bookmark.py` so an import never has to disambiguate which "youtube"
it means.

- `is_youtube_url` / `parse_youtube_url(url) -> str | None`: match
  `(www.|m.)?youtube.com/watch?v=ID`, `youtube.com/shorts/ID`, `youtu.be/ID`.
  Reject playlist-only and channel URLs (no single video ID to key off).
- **No YouTube Data API key.** Use the public, unauthenticated **oEmbed**
  endpoint: `GET https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={id}&format=json`.
  Returns `title`, `author_name` (channel), `thumbnail_url` — no description,
  no API key, no quota. This keeps the handler decoupled from whether the user
  has configured `connectors/youtube.py`'s OAuth credentials: per DESIGN §1.1
  "no implicit escalation," enabling the saved-video connector must not be a
  prerequisite for the Firefox bookmark path, and vice versa.
- Text: `f"{title}\n\nby {author_name}"` (a small local builder, mirroring
  `build_preprint_text`'s shape but without an abstract/pdf argument — no
  shared helper needed for something this small).
- `card_summary`: `None`. oEmbed has nothing to summarize; this matches how
  plain trafilatura-fetched pages already leave `card_summary` unset today —
  not a regression, just no new data to show.
- `title` override: set from oEmbed's `title` (bookmark titles are often
  browser-tab junk like "(3) Some Video - YouTube").
- Failure modes: video is private/deleted/age-gated → oEmbed returns HTTP 401
  or 404 → `unfetchable`. No fallback to scraping the watch page (its
  server-rendered `<meta>` tags are a second source but add complexity for
  little gain over just marking it unfetchable — revisit only if oEmbed
  404-rate turns out high in practice).

**Tests** (`tests/test_youtube_bookmark.py`, mirroring `tests/test_amazon.py`):
URL parsing (watch/shorts/youtu.be/non-video), oEmbed JSON parsing, a
`_fetch_one` integration test with a mocked oEmbed response, and the
401/404 → unfetchable case.

## 3. Reddit — `pka/ingestion/reddit_bookmark.py` (new)

Also deliberately not `reddit.py` (already the connector module).

- `is_reddit_thread_url` / `parse_reddit_permalink(url) -> str | None`: match
  `(www.|old.|np.)?reddit.com/r/<sub>/comments/<id>(/<slug>)?` on
  `reddit.com`/`redd.it` hosts; return the normalized permalink path.
- Fetch: append `.json` to the permalink —
  `GET https://www.reddit.com/r/<sub>/comments/<id>/.json`. Reddit's public
  JSON listing needs no OAuth for a single thread at low volume. Response is a
  2-element array: `[0]` is the post Listing, `[1]` is the comment tree.
- Extract from `data[0].data.children[0].data`: `title`, `selftext` (empty for
  link posts), `is_self`, `url` (external target for link posts), `author`.
  For link posts, use the shared `fetched_embed_text` shape but note the
  *linked* page is a **separate** bookmark/document already handled elsewhere
  in this codebase for the *saved-post* flow (`runners/reddit.py`); here we
  only have the discussion thread itself, so a link post with empty `selftext`
  falls back to the top 3–5 comment bodies (`data[1].data.children[*].data.body`,
  skipping `[deleted]`/`[removed]`/`AutoModerator`) so the thread still carries
  *some* substance instead of just a bare title.
- Text: title + (selftext or top comments), joined via a small local builder
  (same idea as `fetched_embed_text`, not the same function — that one is
  Firefox-runner-specific and takes `(title, card_summary, text)` already
  composed elsewhere).
- `card_summary`: `body_excerpt(selftext)` when there's a selftext, else `None`
  (comments aren't a coherent "summary").
- No `doi`/`year`/`authors_json` — not bibliographic content.

**Risk, and the fallback actually shipped:** Reddit has tightened anonymous API
access since 2023 (third-party app crackdown); the `.json` suffix on a
permalink is still commonly reachable without auth as of this writing, but is
more blockable than arXiv/bioRxiv/Wikipedia's stable public APIs. Rather than
just documenting this and treating every failure as `unfetchable`, a **URL-only
fallback** ships alongside the `.json` path: Reddit encodes an
auto-generated slug in the permalink itself
(`/r/<sub>/comments/<id>/<slug>/`), so when the `.json` call fails (403, 429,
timeout, anything), the handler derives a best-effort title from that slug
(underscores/hyphens → spaces) and pairs it with the subreddit — both already
sitting in the URL, no second HTTP call. This is lossy (lowercased, truncated,
punctuation stripped) but keeps the bookmark searchable by subreddit + rough
topic instead of losing it entirely. The fallback only fires when the URL
actually carries both a subreddit and a slug — a bare `redd.it` short link
(no subreddit in the URL at all) or a slug-less permalink still falls through
to `unfetchable`, since there is nothing to guess from.

**Tests** (`tests/test_reddit_bookmark.py`): permalink parsing (self post, link
post, `old.reddit.com`, `redd.it` short link, non-thread reddit URLs like
`/r/sub/` front pages → `None`), JSON parsing for both self and link posts,
comment-fallback extraction skipping deleted/AutoModerator, and a `_fetch_one`
integration test with a mocked `.json` response.

## 4. PubMed — `pka/ingestion/pubmed.py` (new)

- `is_pubmed_url` / `parse_pubmed_url(url) -> str | None`: match
  `pubmed.ncbi.nlm.nih.gov/<pmid>/`, return the PMID. Scope is PubMed
  **abstract** pages only — full-text PMC articles (`ncbi.nlm.nih.gov/pmc/...`)
  are a different identifier space and out of scope for this TODO item.
- Fetch: NCBI E-utilities `efetch`, no API key required at this call volume
  (rate-limited to 3 req/s by NCBI without a key; the shared `_limiter` is 1
  req/s, comfortably under that) —
  `GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id={pmid}&rettype=abstract&retmode=xml`.
  Parse the `PubmedArticleSet` XML (stdlib `xml.etree.ElementTree`, same
  approach as `arxiv.py`'s Atom parsing) for `ArticleTitle`, `AbstractText`
  (may be multiple labeled sections — join them), `AuthorList` (`LastName` +
  `Initials`), `PubDate/Year`, and `ArticleIdList/ArticleId[@IdType='doi']`.
- No PDF: PubMed itself never hosts full text, so this handler is
  metadata + abstract only — reuses `build_preprint_text(title=, authors=,
  abstract=, pdf_text=None)` as-is (same "abstract only" branch shape as
  arxiv/biorxiv when their PDF fetch fails), rather than writing a fourth
  near-identical text builder.
- Sets `doi` (`normalize_doi(...)` from `pka.ingestion.identifiers`), `year`,
  `authors_json`, `title`, `card_summary = preprint_card_summary(abstract)` —
  same fields, same helpers as arxiv/biorxiv, because it's the same kind of
  document (a paper with a stable ID, an abstract, and no local PDF this time).
- Missing DOI in `ArticleIdList` happens for some older/non-journal entries —
  fall through with `doi=None` rather than treating it as failure; the abstract
  is still worth indexing.

**Tests** (`tests/test_pubmed.py`, directly mirroring `tests/test_biorxiv.py`'s
structure since this is metadata-only like the "PDF unavailable" branch):
PMID parsing, XML parsing (multi-section abstract, missing DOI, malformed XML
→ `None`), and a `_fetch_one` integration test with a mocked efetch response.

## 5. Wiring

### `pka/ingestion/fetcher.py`

Add three dispatch blocks in `_fetch_one_impl`, same shape as the existing
arxiv/biorxiv ones, placed after biorxiv and before the `expect_pdf` check
(`fetcher.py:159`):

```python
from pka.ingestion.youtube_bookmark import fetch_youtube_video, parse_youtube_url
if parse_youtube_url(url):
    result = await fetch_youtube_video(client, doc_id, url)
    if result is not None:
        return result

from pka.ingestion.reddit_bookmark import fetch_reddit_thread, parse_reddit_permalink
if parse_reddit_permalink(url):
    result = await fetch_reddit_thread(client, doc_id, url)
    if result is not None:
        return result

from pka.ingestion.pubmed import fetch_pubmed_article, parse_pubmed_url
if parse_pubmed_url(url):
    result = await fetch_pubmed_article(client, doc_id, url)
    if result is not None:
        return result
```

`_fetch_one`'s import list (`fetcher.py:230`) and the module docstring
(`fetcher.py:1-23`) get one line each, matching the existing arxiv/biorxiv/
wikipedia entries.

### `pka/domains.py`

Add the three predicates to `domain_has_fetch_handler` (`domains.py:35`),
alongside the existing four:

```python
from pka.ingestion.pubmed import is_pubmed_url
from pka.ingestion.reddit_bookmark import is_reddit_thread_url
from pka.ingestion.youtube_bookmark import is_youtube_url
...
return (
    is_wikipedia_url(probe)
    or is_amazon_host(probe)
    or is_arxiv_url(probe)
    or is_biorxiv_url(probe)
    or is_youtube_url(probe)
    or is_reddit_thread_url(probe)
    or is_pubmed_url(probe)
)
```

This was flagged as follow-up work in `planning/DOMAIN_TOP_LISTS_PLAN.md` §8
when that report shipped — this is where it gets paid off.

## 6. Docs

- **`docs/ingestion-flows.md` §2 Firefox** (per `CLAUDE.md`'s sync rule — this
  changes what the dispatch pool does, not just adds a node deep inside a
  handler): add `YT`, `RDT`, `PMD` nodes beside `ARX`/`BIO` at lines 279-280,
  wired from `DISPATCH` the same way, and extend the `_fetch_budget_seconds`
  label at line 273 only if a reviewer would otherwise wonder why 3 more
  handlers didn't need new budget terms (they don't — one request each, see
  §1 above) — a one-line note is enough, not a diagram change.
- Same pool is reused by Reddit's own link-post flow (§3.2 / line 532,
  `docs/ingestion-flows.md`) — update that node's label list too, since it
  says "identical to Firefox" and must stay true.
- `DESIGN.md` §1.1's connector table already covers this generically
  ("Firefox phase-2 fetch... fetch a URL you bookmarked") — no change needed,
  these three don't introduce a new category.
- `planning/TODO.md`: tick all four source-connector lines, point the three
  new ones at this file the way other worked-out items do, and reword/tick
  the Amazon line per §0.

## 7. Suggested order

1. Amazon regression test + TODO cleanup (§0) — trivial, no design risk, ships
   first.
2. PubMed — closest existing precedent (arxiv/biorxiv "abstract only" branch,
   reuses `build_preprint_text` verbatim), lowest design risk.
3. YouTube — smallest handler (single oEmbed call, no XML/JSON tree walking).
4. Reddit — most moving parts (self vs. link posts, comment fallback, the
   external blocking risk in §3) — do last so the shared plumbing (dispatch
   wiring, `domains.py`, docs pattern) is already proven by the first two.

Verify each with `pytest`, then run the full suite once at the end; no
frontend changes, so `npm run test` / `npm run build` are unaffected.
