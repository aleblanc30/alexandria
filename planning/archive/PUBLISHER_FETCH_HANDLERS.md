# Publisher fetch handlers: doi.org, Nature, Springer, APS, ScienceDirect, MIT Press, ResearchGate

**Status:** implemented, 2026-09-03. Kept as the record of *why* each handler
is shaped the way it is; the code in `pka/ingestion/` is what it does now.
Two things shipped differently from the text below. **§13's open question is
now answered:** `direct.mit.edu` was probed on 2026-09-03 and returns `403` to a
non-browser client on both an article page and a book page, so the
`citation_doi` scrape §9 floated is not a route — a meta tag cannot be read out
of a page that never loads. It got a handler anyway
(`pka/ingestion/direct_mit.py`), but not the one §9 imagined. Its article URLs
carry volume, issue and first page — the same numbers that appear in a legacy
`10.1162` DOI suffix, though the year does not and modern deposits
(`10.1162/neco_a_01227`) abandon the pattern — so the DOI is **searched** with a
`query.bibliographic` call scoped by `filter=prefix:10.1162` and then **verified**
against those three coordinates. Its `article-pdf` URLs are cheaper still: the
filename is the DOI suffix verbatim, in both the legacy (`neco.1997.9.8.1735`)
and modern (`neco_a_01227`) forms, so those need no search at all — just the
prefix, one lookup, and the same coordinate check. That verification is what
makes a ranked query admissible here and inadmissible in §10.1: an RG slug has nothing to round-trip
against, a citation does. Its book URLs carry only a title, which goes to
`openlibrary.lookup_by_title_author` — the existing verified ladder, on the
existing `external_lookup_enabled`. Either shape falls back to a slug card
rather than to the blocked GET. Second, the four DOI handlers share one
`fetch_doi_card` entry point in `doi_meta.py` rather than repeating the lookup
body per module.
**Touches:** `pka/ingestion/doi_meta.py`, `doi_org.py`, `nature.py`, `springer.py`,
`aps.py`, `sciencedirect.py`, `mitpress.py`, `researchgate.py` (all new),
`pka/ingestion/fetcher.py`, `pka/ingestion/fetch_base.py`, `pka/domains.py`,
`pka/config.py`, `docs/ingestion-flows.md`, `docs/persisted-fields.md`.

Covers seven `planning/TODO.md` items under *Source connectors*:

> - **nature.com fetch handler** — needs a dedicated Firefox fetch handler (paywall/anti-bot page currently scraped as-is).
> - **doi.org fetch handler** — DOI redirect target isn't resolved/handled, so the landing page is scraped as-is.
> - **sciencedirect.com fetch handler** — top unfetchable domain; needs a dedicated Firefox fetch handler.
> - **link.springer.com fetch handler** — needs a dedicated Firefox fetch handler alongside the other publisher domains above.
> - **mitpress.mit.edu fetch handler** — top unfetchable domain; needs a dedicated Firefox fetch handler.
> - **journals.aps.org fetch handler** — top unfetchable domain; needs a dedicated Firefox fetch handler.
> - **researchgate.net fetch handler** — top unfetchable domain; needs a dedicated Firefox fetch handler.

They are one plan because five of the seven collapse onto the same spine —
*derive a DOI from the URL, then look the DOI up* — and writing them separately
would specify that spine five times. §2–§4 are shared; §5–§11 are one section
per handler, each carrying only what is genuinely its own. §10 and §11 are the
two that leave the DOI family entirely, and that is the most important thing
this plan establishes: **"publisher domain" is not one problem.**

## 1. Two different failure modes, not one

The TODO lines all say "scraped as-is", but the domains split into two groups
that fail in opposite ways.

**Loud failure — `journals.aps.org`, `mitpress.mit.edu`, `researchgate.net`.**
These answer an unauthenticated non-browser client with `403`. `_fetch_one_impl`
reaches `if http_status >= 400` (`pka/ingestion/fetcher.py:216`) and writes
`unfetchable` with reason `HTTP 403`. Nothing bad enters the archive; the
bookmark just has no title, no abstract, and no chunks. This is why these three
rank on the *top unfetchable* list — the list is what surfaced them.
(`mitpress.mit.edu/9780262048613/the-alignment-problem/` returns 403 to a plain
fetch; confirmed while writing this.)

**Silent failure — `nature.com`, `link.springer.com`, `sciencedirect.com`,
`doi.org`.** These return `200` with a paywall or cookie wall, and
`_extract_text` (`pka/ingestion/fetch_base.py`) dutifully extracts it. The
document is marked `fetched`, gets chunked, and gets embedded. The archive now
contains a chunk about institutional subscription pricing filed under the title
of a paper on protein folding. This is worse than the loud case, and it is
**invisible in the unfetchable report** — these domains rank high on documents,
not on failures, so nothing flags them.

Both are avoidable for the same reason: a scholarly URL carries a resolvable
identifier, and that identifier has free structured metadata behind it. The
publisher's HTML is not needed at all.

## 2. The shared spine — `pka/ingestion/doi_meta.py`

### 2.1 Two endpoints, one ladder

```
GET https://doi.org/{doi}                       Accept: application/vnd.citationstyles.csl+json
GET https://api.crossref.org/works/{doi}
```

| Function | Endpoint | Used by |
|---|---|---|
| `resolve_doi_negotiated(client, doi)` | `doi.org` + `Accept:` CSL-JSON | §5 only |
| `fetch_crossref_work(client, doi)` | `api.crossref.org/works/{doi}` | §6–§9 |
| `fetch_doi_metadata(client, doi, *, negotiated=False)` | picks one, then §2.2 | all of them |

Content negotiation is the right primary for a **`doi.org` bookmark** because
the bookmarked host *is* `doi.org` — no third-party question arises (§3) — and
because it is registration-agency agnostic, so a DataCite DOI (Zenodo dataset,
figshare item) answers where a Crossref-only client would 404. The publisher
handlers use `api.crossref.org` directly because they need query support that
content negotiation does not offer; §8 depends on it outright.

### 2.2 The abstract ladder is not optional — measured, not assumed

Crossref abstract coverage is **per-deposit, not per-publisher**. Checked
against live records while writing this plan:

| Sample DOI | Publisher | Crossref abstract? |
|---|---|---|
| `10.1038/s41586-020-2649-2` | Springer Nature (*Nature*) | ✅ present |
| `10.1103/PhysRevLett.116.061102` | APS | ✅ present |
| `10.1162/neco.1997.9.8.1735` | MIT Press | ✅ present |
| `10.1007/s11263-015-0816-y` | Springer (*IJCV*) | ❌ absent |
| `10.1016/j.artint.2018.07.007` | Elsevier | ❌ absent |

Elsevier is the systematic case — it deposits no abstracts to Crossref — but
the Springer row shows the gap is not cleanly publisher-shaped: the same
publishing group has abstracts under `10.1038` and none under `10.1007`. So a
per-publisher "does Crossref suffice" table would be a lie, and **every handler
needs the fallback.**

OpenAlex was checked as that fallback and **rejected**: it returned no abstract
for `10.1016/j.artint.2018.07.007` either, its Elsevier abstracts being
withheld for the same licensing reason. The Semantic Scholar Graph API returned
the full abstract:

```
GET https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}
    ?fields=title,abstract,year,authors,externalIds,openAccessPdf
```

No key for low volume. **Called only when the primary record has no abstract**,
never speculatively, so the common case stays at one request.

### 2.3 `DoiMetadata`

```python
@dataclass(frozen=True)
class DoiMetadata:
    doi: str            # normalized via identifiers.normalize_doi
    title: str
    authors: list[str]  # "Given Family", assembled from Crossref author objects
    abstract: str | None
    year: int | None
    container: str | None   # container-title[0] — journal or book series
    type: str | None        # journal-article | book-chapter | posted-content | dataset
    arxiv_id: str | None    # externalIds.ArXiv from the S2 leg, when present
```

`abstract` stays `str | None`: a metadata-only card is a real, acceptable
outcome — `pubmed.py` already ships one shape of it — and a handler treating a
missing abstract as failure would mark half of Elsevier unfetchable.

Two parsing details, pinned here because they will otherwise be found by a user
rather than a test:

- **Crossref abstracts are JATS XML**, arriving as `<jats:p>Text…</jats:p>`
  inside a JSON string. Strip to text the way `_extract_text` does as its last
  resort, and drop a leading `Abstract` heading — never embed raw tags.
- **`title` is an array.** Take `[0]`; a missing or empty array is a failed
  lookup, not an empty title.

### 2.4 What every DOI handler returns

Reuse the existing preprint assembly rather than inventing a fourth text
builder — `build_preprint_text` (`pka/ingestion/preprint_text.py`) already
takes exactly `title` / `authors` / `abstract` / `pdf_text`:

```python
text = build_preprint_text(
    title=meta.title, authors=meta.authors,
    abstract=meta.abstract or "", pdf_text=None,
)
```

- `title` ← `meta.title`. The browser-supplied bookmark title on these domains
  is usually the paywall's `<title>` or a redirect stub.
- `card_summary` ← `preprint_card_summary(meta.abstract)` (`pka/card_summary.py`),
  which already returns `None` for a blank abstract — so the no-abstract case
  needs no branch.
- `doi`, `year`, `authors_json`, `arxiv_id` ride existing `FetchResult` fields
  (`fetch_base.py:28`); `_persist_fetch_result` (`fetcher.py:329`) already
  writes all four. **No schema change and no new `FetchResult` field** — except
  §10, which needs `isbn`.
- `error_msg` carries provenance the way every sibling handler does:
  `"fetched via <host> → crossref"`, or `"… (abstract from semantic scholar)"`
  when the ladder took its second rung.

Failure → `FetchResult(doc_id, url, "unfetchable", None, http_status, reason)`.
**Do not fall through to the generic GET on a metadata miss:** that reinstates
exactly the paywall-scrape these handlers exist to remove. A DOI that resolves
to nothing is a dead bookmark and should read as one.

`item_type` is deliberately **not** written from `DoiMetadata.type`. Per
`docs/persisted-fields.md` that column has two writers today (Zotero, Reddit)
with their own vocabularies; adding a third is a change to the column's
meaning, not a handler detail. If wanted, it spans all five DOI handlers at
once and belongs in `archive/DOCUMENT_METADATA_PLAN.md`.

## 3. Config and the DESIGN.md §1.1 question

The two cases are genuinely different and the plan should not blur them.

**§5 (`doi.org`) needs no new flag.** It sits in §1.1's *Source connectors →
"fetch a URL you bookmarked"* bucket, exactly like the `export.arxiv.org` call
for an `arxiv.org` bookmark: the request goes to the host that was bookmarked.

**§6–§9 do not get that argument for free.** A `nature.com` bookmark triggering
a call to `api.crossref.org` is a request to a *third party the user did not
bookmark*, disclosing one derived identifier — the DOI. That is §1.1's
*Enrichment lookups* category (*reveals library inventory rather than
content*), the same bucket as `external_lookup_enabled`. So:

```python
doi_metadata_lookup: bool = True   # Crossref / Semantic Scholar for a publisher DOI
```

listed in the `_parse_bool` validator alongside `fetch_wayback_fallback`
(`pka/config.py:436`). Default **on** is defensible and should be argued rather
than assumed: what crosses the wire is a DOI the user bookmarked, in plain
text, to a non-commercial scholarly registry — and the flag exists so it can be
turned off. `fetch_wayback_fallback` is the existing default-on outbound
precedent. If that reads as too loose, default it off and the handlers degrade
to §8.5's metadata-less card, which is already specified.

§5 checks the flag too, for a narrower reason: with it off, the Semantic
Scholar rung must not fire even though the `doi.org` rung may.

§10 rides the **existing** `external_lookup_enabled` (default off) — it is an
Open Library ISBN lookup, not a DOI lookup. §11 adds no outbound path at all.
No implicit escalation: none of these three flags enables another.

## 4. Shared wiring — do this once

**Budget.** `_fetch_budget_seconds` (`fetcher.py:99`) gains a `doi` leg. Worst
case is two sequential requests, no PDF:

```python
if doi:
    base += cfg.fetch_timeout_seconds + cfg.fetch_connect_timeout_seconds
```

mirroring the first half of the `preprint` leg. `_fetch_one` (`fetcher.py:255`)
computes the flag the way it already computes `preprint` — by re-running the
parsers. §11 needs no budget at all (no request); §10 needs one only when
`external_lookup_enabled` is on.

**Dispatch.** In `_fetch_one_impl`, one block per handler after the pubmed block
(`fetcher.py:182-188`) and before `expect_pdf`, in the standard shape:

```python
if parse_nature_url(url):
    result = await fetch_nature_article(client, doc_id, url)
    if result is not None:
        return result
```

Order *within* the publisher block is free — the host checks are disjoint.
Order relative to **arXiv** is not: `doi.org/10.48550/arXiv.2301.00001` is a
valid arXiv DOI and `arxiv.py` yields full PDF text where §5 yields an
abstract. Since `parse_arxiv_url` matches on `arxiv.org` hosts, §5 handles the
cross-walk itself — on a `10.48550/arxiv.` prefix, hand off to
`fetch_arxiv_paper` with the reconstructed `arxiv.org/abs/<id>` URL.
`identifiers.derive_arxiv_doi` documents that mapping in the other direction.

**`pka/domains.py`.** Every handler needs its mirror predicate added to
`domain_has_fetch_handler`'s `or` chain (`domains.py:56`), or the domain report
keeps listing the domain as unhandled and the next reader re-opens the TODO.
Eight predicates land here: `is_doi_host`, `is_nature_url`, `is_springer_url`,
`is_aps_url` (which must match **both** APS hosts, §7), `is_sciencedirect_url`,
`is_mitpress_url`, `is_researchgate_url`. All are host-only, so index pages that
fall through inside a handler still report as handled — the same imprecision
`is_amazon_host` and `is_search_engine_host` already carry, per the comment at
`domains.py:60`.

**A shared parsing primitive.** §6–§9 all decode a DOI out of a path. Put one
helper in `doi_meta.py` and use it from each:

```python
def doi_from_path(path: str, *, prefix: str | None = None) -> str | None:
    """Percent-decode, find the first `10.dddd` segment, take it and the rest."""
```

Positional, not enumerated: it handles every URL shape in §6–§9 plus shapes not
yet invented, and needs no edit when a publisher adds a new content type. It
strips a trailing `.pdf` and a trailing `/`, and it `unquote`s **before**
segmenting — `/article/10.1007%2Fs11263-015-0816-y` is a real browser-produced
shape, and without the early `unquote` the scan sees one segment and finds no
`10.xxxx` at all.

## 5. `doi.org` — the thinnest consumer

`pka/ingestion/doi_org.py`:

```python
def is_doi_host(url: str) -> bool
def parse_doi_url(url: str) -> str | None
```

Match `doi.org` and legacy `dx.doi.org`, with or without `www.`. The DOI is
everything after the leading `/` — a **prefix strip, not a path split**, because
DOI suffixes contain `/` (`10.1103/PhysRevLett.116.061102`). Percent-decode
once. Require a `10.\d{4,9}/` prefix; `doi.org/`, `doi.org/about` → `None` and
fall through.

Send the DOI **as parsed**, not lowercased. Resolution is case-insensitive so it
does not matter to the request, but `normalize_doi` lowercases for the
`documents.doi` column, and mixing the two invites a later "simplification"
that reuses the lowercased value in a URL where a future agency might care.

`identifiers.py` already owns `normalize_doi` and its
`https?://(dx\.)?doi\.org/` regex (`_DOI_PREFIX_RE`) — reuse it for the column
value rather than adding a second DOI regex.

`hdl.handle.net` is **out of scope**: it is the wider Handle system, DOIs are
one namespace inside it, and non-DOI handles have no metadata contract.
Revisit only if it appears in the domain report.

Two `doi.org`-specific defects this also fixes, worth recording:

- The rate-limiter slot is claimed against `doi.org`, not the host the redirect
  lands on, so a folder of `doi.org/10.1016/…` links hammers
  `sciencedirect.com` with no per-domain spacing. `FETCH_DISPATCH_PLAN.md`'s
  `_throttle_key` has the same redirect blind spot and should say so.
- On a 404, `fetch_wayback_fallback` (`fetcher.py:212`) queries archive.org for
  a snapshot of the *`doi.org` URL* — a redirect stub, never the content
  anyone wanted.

## 6. `nature.com` — concatenate a constant prefix

Nature article URLs carry the DOI suffix verbatim, and the prefix `10.1038` is
constant across everything Springer Nature publishes on this host. **No lookup
step**: the mapping is string concatenation.

Host `^(www\.)?nature\.com$`, with an optional leading journal segment
(`nature.com/<journal>/articles/…`). Three path shapes:

| Shape | Example | DOI |
|---|---|---|
| Modern | `/articles/s41586-020-2649-2` | `10.1038/s41586-020-2649-2` |
| Legacy numbered | `/articles/nature12373` | `10.1038/nature12373` |
| Legacy journal tree | `/nature/journal/v491/n7422/full/nature11421.html` | `10.1038/nature11421` |

The third is a real archive shape — bookmarks outlive site redesigns — and is
the only one needing more than a prefix strip: take the basename, drop `.html`,
drop a trailing `_S1`/`_F1` supplementary marker.

Accept the article-ID alphabet conservatively — `^[A-Za-z0-9._-]+$`, rejecting
any further `/`. That one rule excludes every non-article path without
enumerating them (`/subjects/genetics`, `/nature/volumes/491`, `/collections/…`),
and those then fall through to the generic path **unchanged, which is correct**:
a Nature subject index is a real, fetchable, non-paywalled page that trafilatura
handles fine.

**News items are in scope, deliberately.**
`nature.com/articles/d41586-020-02462-7` is a *Nature* news piece and its
`d41586-` DOIs are registered with Crossref. They often carry no abstract —
fine, §2.4's card degrades to metadata-only. They are a large share of what
lands in a bookmark folder from this host, and a title + byline + year card
beats a paywall scrape.

**No PDF attempt.** `nature.com/articles/<id>.pdf` is paywalled for non-OA
content and returns an HTML interstitial at HTTP 200, which `_fetch_pdf_result`
then rejects as *"response is not a PDF"* — a wasted request and a misleading
`fetch_log` row.

`scientificamerican.com` is **excluded**: same publisher, but its content has no
DOI and trafilatura reads it correctly.

## 7. `journals.aps.org` — DOI in the path, plus a second host

Two hosts, both in scope:

| Host | Shape | Example |
|---|---|---|
| `journals.aps.org` | `/<journal>/<view>/<DOI>` | `/prl/abstract/10.1103/PhysRevLett.116.061102` |
| `link.aps.org` | `/<view>/<DOI>` | `/doi/10.1103/PhysRevLett.116.061102` |

`link.aps.org` is APS's own redirector, common in citation lists and reference
managers. It is *not* `doi.org` and §5 will not match it — handle it here
rather than leaving a second unfetchable domain nobody opened a TODO for.

Views in the wild: `abstract`, `pdf`, `accepted`, `supplemental`, `cited-by`,
`references`, `export`, and `doi` on `link.aps.org`. Journal slugs: `prl`,
`pra`…`prx`, `rmp`, `prper`, `prapplied`, and more. `doi_from_path` (§4)
handles all of them, so neither list needs enumerating or maintaining.

Constrain the prefix to `10.1103` after the scan. APS mints nothing else, and
the check turns a malformed path into a clean fall-through instead of a request
for a DOI that cannot exist.

**Supplemental material is a deliberate merge, not a bug:**
`/supplemental/10.1103/PhysRevLett.116.061102` resolves to the article record,
so its card describes the paper, not the supplement. That is the right answer —
the supplement has no independent metadata — but two bookmarks can then produce
two documents with identical titles. `source_id` is the bookmark id
(`docs/persisted-fields.md`), so they stay distinct rows; say this in the module
docstring so the duplicate is understood rather than "fixed".

One data wrinkle: Crossref's APS abstracts end with *"Published by the American
Physical Society 2016"* and render inline mathematics as spaced Unicode
(`1.0 × 10 − 21`). Neither is worth special-casing —
`preprint_card_summary` does no cleaning for arXiv or PubMed either, and
per-publisher scrubbing rules are how this module starts growing. Recorded here
so a reviewer seeing odd spacing on a card knows it is upstream.

### 7.1 Optional later slice: the arXiv cross-walk

**Ship §7 first.** Nearly every APS paper has an arXiv preprint, and
`pka/ingestion/arxiv.py` already fetches **full PDF text**, not just an
abstract. So an APS bookmark could yield a whole paper:
`DoiMetadata.arxiv_id` (from S2's `externalIds.ArXiv`) → `fetch_arxiv_paper`,
overriding `doi` with the *published* DOI so the archive keys off the version of
record. `identifiers.resolve_doi` already encodes "a source-provided DOI always
wins".

Three reasons it is separate:

- **Cost.** It forces the S2 request on every APS URL — doubling requests for
  the case §7 already handles well — then adds an arXiv API call and a PDF
  download: three or four requests where one sufficed.
- **Fidelity.** The arXiv version is not the version of record. Storing it under
  the journal DOI is a provenance claim the archive cannot qualify —
  `documents` has no manuscript-version column, and adding one is a
  `archive/DOCUMENT_METADATA_PLAN.md` change.
- **Scope.** The cross-walk is not APS-specific; building it inside `aps.py`
  guarantees it gets copy-pasted four times. If it ships it belongs in
  `doi_meta.py` behind `doi_arxiv_crosswalk: bool = False`, with each handler
  opting in by one argument.

## 8. `link.springer.com` and `sciencedirect.com` — read it vs. resolve it

These two are the extremes of the derivation problem and are best read together.

### 8.1 Springer: the DOI is simply there

`https://link.springer.com/article/10.1007/s11263-015-0816-y` — prefix and
suffix both in the path. `doi_from_path` (§4) and go. Host
`^(www\.)?link\.springer\.com$`; content segments seen in the wild are
`/article/`, `/chapter/`, `/book/`, `/referenceworkentry/`, `/protocol/`,
`/content/pdf/` (suffix ends `.pdf`), `/epdf/`, `/full/`. The positional scan
covers all of them and any future addition.

Book DOIs contain hyphens and underscores (`978-3-030-01234-5_7`); do not
"clean" the suffix beyond the trailing `.pdf` and `/` strips.

Reject and fall through for `/journal/11263`, `/search?…`, `/collections/…`,
and the bare host — index pages the generic path handles correctly.

`/content/pdf/` is in scope rather than excluded, and the asymmetry is worth
noting: when the *bookmark itself* is a `/content/pdf/…` URL,
`_url_looks_like_pdf` is true and today's generic path already attempts the PDF
route and fails on the HTML interstitial. Intercepting it and returning a real
metadata card is a strict improvement.

Excluded hosts: `springer.com` (marketing, no DOIs), `springeropen.com` and the
BMC hosts (open access — the generic path already gets full text, and replacing
that with an abstract would be a regression).

**Springer is the evidence that §2.2's ladder is mandatory.** Crossref returned
no abstract for `10.1007/s11263-015-0816-y` while returning one for the
`10.1038` sample from the same publishing group. Build this handler as
"Crossref only, it's simpler" and a large slice of SpringerLink bookmarks land
as abstract-less cards, the handler looks half-broken, and the cause — a
per-deposit metadata gap — is invisible.

### 8.2 ScienceDirect: an opaque PII, and a real resolution route

`https://www.sciencedirect.com/science/article/pii/S0004370218305988` — a PII,
not a DOI. Nothing in it derives `10.1016/j.artint.2018.07.007`; the mapping is
not computable.

It **is** queryable. Elsevier deposits the PII to Crossref as an
`alternative-id`, and the REST API filters on it:

```
GET https://api.crossref.org/works?filter=alternative-id:S0004370218305988&rows=1&select=DOI,title
```

Verified live: exactly one item, `10.1016/j.artint.2018.07.007`. This is the
one place a publisher handler needs query support rather than
content negotiation (§2.1).

**And it is only a partial route.** The same query for the older PII
`S0004370200000521` returned `total-results: 0` — Elsevier's `alternative-id`
deposits are not retroactive. So the handler has a real miss rate concentrated
in older bookmarks, and §8.5 has to say what happens then.

Host `^(www\.)?sciencedirect\.com$`, plus `linkinghub.elsevier.com`, whose
`/retrieve/pii/<PII>` form is the same identifier and appears wherever a
reference manager wrote the link. Path shapes: `/science/article/pii/<PII>`,
`/science/article/abs/pii/<PII>`, `/science/article/pii/<PII>/pdf(ft)`.

`^[SB]\d{8,}[0-9X]$` is the PII alphabet — `S` for serials, `B` for book
chapters. Reject `/journal/artificial-intelligence`, `/search?…`, and the bare
host.

### 8.3 ScienceDirect needs both rungs, for both reasons

Elsevier deposits no abstracts to Crossref at all (§2.2), so **every**
ScienceDirect document takes the Semantic Scholar rung. That makes the common
case here three requests, not two: PII→DOI, Crossref work, S2 abstract.

Worth collapsing to two: the `filter=alternative-id` query can `select` the
full record in one call rather than resolving the DOI and then fetching it
again. Do that — `select=DOI,title,author,issued,container-title,type` — and
treat the single-work `fetch_crossref_work` as the fallback when the filter
query returns a thin record.

### 8.4 Budget for ScienceDirect

Three sequential requests exceeds §4's one-extra-request `doi` leg. Give it its
own flag rather than inflating the leg for the four handlers that do not need
it:

```python
if pii:
    base += 2 * (cfg.fetch_timeout_seconds + cfg.fetch_connect_timeout_seconds)
```

### 8.5 When the PII does not resolve

The old-PII miss (§8.2) is the shape this must handle, and there are three
options. Take the third:

1. **Fall through to the generic GET.** Rejected — that is the silent paywall
   ingest of §1, which is the whole reason for the handler.
2. **`unfetchable`, reason `"pii not resolvable to a doi"`.** Honest, and
   `reset_unfetchable_for_fetch` (`fetcher.py:290`) will re-queue it once the
   retry cooldown elapses, so a later Elsevier backfill would pick it up. But
   it leaves the document with no title at all.
3. **A URL-derived card**, precedent `reddit_bookmark._url_fallback_result`
   (`reddit_bookmark.py:188`) and `search_url.py`: `status="fetched"`,
   `http_status=None`, `title` from the bookmark's own title where present,
   `card_summary` naming the source and PII, `error_msg` recording
   `"sciencedirect pii unresolved; card built from url"`. The document is
   findable and honestly labelled.

Option 3 needs one caveat in the docstring: `status="fetched"` means
`source_ingest_queue` (`pka/db/queries.py`) will not re-queue it, so a future
Elsevier deposit backfill will not be picked up automatically. That is the same
trade `search_url.py` §5 already accepted, and for the same reason — a novel
`fetch_status` silently drops the document out of orphan backfill.

## 9. `mitpress.mit.edu` — not a DOI handler at all

This is the item most likely to be built wrong by analogy with its six
neighbours. `mitpress.mit.edu` is **a bookstore**. Its pages are book product
pages with no DOI anywhere:

```
https://mitpress.mit.edu/9780262369466/advanced-microeconomics-…/
                         └─── ISBN-13 ───┘ └──── title slug ────┘
```

Confirmed shapes: `/9780262533256/raised-to-rage/`, `/9780262536332/spaceflight/`,
and the slugless `/9780262192026/`.

So the identifier is an **ISBN**, and the archive already has a complete,
tested, verified ISBN resolution ladder — `pka/ingestion/openlibrary.py`, whose
`lookup_by_isbn` (`openlibrary.py:230`) is checksum-validated first and returns
a `BookSynopsis`. This handler is a sibling of `amazon.py`, not of §6–§8.

Four things follow, all of them specific to this section:

**It crosses the sync/async boundary.** `openlibrary.py` is deliberately
synchronous with its own `SyncRateLimiter`, because its existing callers
(`image_pipeline.ingest_image`, `runners/calibre`) are synchronous. The fetch
pool is async. Call it through `asyncio.to_thread`, exactly as
`_fetch_one_impl` already does for `_fetch_pdf_result` (`fetcher.py:229`) and
`extract_amazon_book` (`fetcher.py:234`). Do **not** rewrite `openlibrary.py`
as async to suit one new caller.

**It needs a new `FetchResult` field.** `documents.isbn` exists but
`docs/persisted-fields.md` shows it as `—` for Firefox: `FetchResult`
(`fetch_base.py:28`) has no `isbn`, so `_persist_fetch_result`
(`fetcher.py:329`) cannot write one. Add `isbn: str | None = None` to the
dataclass and the matching `if r.isbn:` branch — the same one-line shape as
`doi`, `year`, and `authors_json`. This is the only schema-adjacent change in
the whole plan, and it flips a cell in the §1 matrix from `—` to `⬛`.
Validate with `openlibrary.normalize_isbn` / `isbn_checksum_valid`
(`openlibrary.py:96`, `:110`) before writing — footnote ⁴ of
`docs/persisted-fields.md` says a bad-checksum ISBN is rejected rather than
stored, because it is a join key.

**It is gated off by default, and that is correct.**
`external_lookup_enabled` defaults to `False` (`pka/config.py:396`). So on a
fresh checkout this handler makes **no request at all** and still improves on
today's 403: title from the slug (hyphens → spaces, title-cased), ISBN from the
path, `status="fetched"`, `http_status=None` — the §8.5 option-3 shape again.
The Open Library lookup is a strict upgrade layered on top when the flag is on.
Per DESIGN §1.1 this needs no *new* flag; it is another consumer of an existing
one, and no implicit escalation occurs.

**`direct.mit.edu` is a different host and belongs to §6–§8.** MIT Press's
journals and books *gateway* mints `10.1162` DOIs — Crossref abstract confirmed
present for `10.1162/neco.1997.9.8.1735` (§2.2). The TODO names only
`mitpress.mit.edu`, but the two hosts split cleanly: `mitpress.mit.edu` →
ISBN/Open Library, `direct.mit.edu` → `doi_from_path` + `doi_meta`. Build both;
they are two small handlers, not one confused one. `direct.mit.edu` article
paths are `/<journal>/article/<vol>/<issue>/<page>/<id>/<slug>`, which carries
**no DOI** — so for that host, derive nothing from the path and instead read the
DOI from the page's `citation_doi` meta tag, or leave it to fall through. Flag
this as the one unresolved question in the plan (§13).

## 10. `researchgate.net` — a no-network handler

ResearchGate is hard-blocked: Cloudflare bot protection, `403` to anything that
is not a real browser, and no public API. Every technique that would get past
it — TLS/JA3 fingerprint spoofing, a headless browser, proxy rotation — is
**anti-bot evasion**, which this plan does not propose and which does not
belong in a local-first research archive. Getting content out of ResearchGate
is not the goal; making the bookmark useful is.

And the bookmark already carries the payload, in the slug:

```
researchgate.net/publication/334080242_403_Forbidden_A_Global_View_of_CDN_Geoblocking
                             └──ID───┘ └──────────── title, underscored ────────────┘
```

So this is the `search_url.py` pattern, not the `arxiv.py` pattern: a
**synchronous handler that takes no `client` and makes no request**.

```python
def is_researchgate_url(url: str) -> bool
def researchgate_result(doc_id: int, url: str) -> FetchResult | None
```

- Match `^(www\.)?researchgate\.net$`, path `^/publication/(\d+)_(.+)$`.
- Title: underscores → spaces, collapse whitespace, `truncate_summary`
  (`pka/card_summary.py`) so a pathological slug cannot become a 40 KB title.
  Leave the casing as-is — RG slugs preserve the paper's original capitalisation,
  and title-casing it would corrupt acronyms (`CDN`, `403`).
- `card_summary`: `"ResearchGate publication: <title>."` Set explicitly, so
  `embed_fetched_text` does not fall back to `body_excerpt(text)`
  (`runners/firefox.py:104`).
- `text`: `f"{title}\n\nResearchGate publication"` — short by design; there is
  nothing else true to say.
- `status="fetched"`, `http_status=None`, `error_msg="researchgate;
  card built from url slug, no fetch"`. `fetched` rather than a novel status,
  for `search_url.py` §5's reason: `source_ingest_queue` re-queues only
  `pending` and `fetched`-missing-chunks, so a new status would silently drop
  the document out of orphan backfill.

Other RG path shapes — `/profile/<Name>`, `/figure/…`, `/post/…`,
`/institution/…` — return `None` and fall through. They are not documents, and
they will fail with `403` as they do today; that is the correct outcome and
this handler should not pretend otherwise.

Dispatch **first**, next to `search_url_result` at `fetcher.py:146`, and
un-`await`ed — it is sync while every neighbour is awaited, exactly as
`search_url_result` already is. No budget leg, no config flag, no rate-limiter
slot.

### 10.1 Rejected: title-matching the slug against Crossref

The obvious upgrade is to feed the decoded title to
`api.crossref.org/works?query.bibliographic=…` and attach the real abstract.
Rejected for this slice, on the archive's own precedent:
`openlibrary.py`'s docstring already argues it, for exactly this failure —
*"Accepting rank 1 unverified is how you attach the wrong book's synopsis to a
document, which is worse than attaching none — it shifts `doc_embedding` and
makes the document findable under the wrong queries."*

If it is ever built, it must follow that module's shape rather than skipping it:
a verified round-trip via `titles_match` (`openlibrary.py:151`) and
`authors_match` (`:173`), its own flag defaulting **off**, and an unverified
match discarded rather than downgraded. That is a second slice, not a footnote
to this one.

## 11. Doc sync — mandatory, same commit (CLAUDE.md)

**`docs/ingestion-flows.md`.** Add one node per handler to the `handlers`
subgraph (`ingestion-flows.md:275-297`) with a `DISPATCH --> …` edge, and
extend the one-line handler list at `:543`. The colour classes are the point of
the file, so get them right: §5–§9 are **external** (red) and, once §3's flag
lands, **gated** (purple dashed); §9's Open Library leg is **gated** on
`external_lookup_enabled`; §10 is **specific** (orange) with no external node at
all — it makes no request, and drawing it red would be a lie. The Reddit graph
defers with "identical to Firefox" and needs no edit.

**`docs/persisted-fields.md`.** Two edits, both required:

- Footnote ³ (`persisted-fields.md:57`) reads *"Set only by the arXiv/bioRxiv
  fetch handlers"* and is **already stale** — `pubmed.py` sets `doi`, `year` and
  `authors_json` too. Correct it to name the handler family rather than
  enumerating members, so the next handler need not touch it again.
- `isbn` flips from `—` to `⬛` in the Firefox column (§9), with a footnote
  pointing at `openlibrary.normalize_isbn` and the existing checksum rule.

## 12. Tests

One file per handler, following `tests/test_pubmed.py`'s shape — fixture
payload at module scope, `AsyncMock` client, no network (`tests/conftest.py`
already blocks it). Per-file, the parsing tables above turn directly into
parametrised cases; below are only the tests that carry an argument.

**`tests/test_doi_meta.py` — the ladder.** Primary-with-abstract makes exactly
**one** request (assert the mock's `call_count`, not just the result);
primary-without-abstract makes two and the abstract comes from the second; an
S2 404 leaves a metadata-only `fetched` result, not `unfetchable`;
`doi_metadata_lookup=False` suppresses the second rung. Plus JATS stripping
(`<jats:p>` wrapper, nested `<jats:italic>`, leading `Abstract` heading) and the
title-array rule.

**`doi_from_path` (§4).** The percent-encoded Springer form resolves to the same
DOI as the plain form — the assertion most likely to regress if someone reorders
`unquote`. And the **positional-scan property**: an invented segment
(`/livingreferenceentry/10.1007/978-…`) still parses. That test is what stops a
later "tidy-up" from replacing the scan with a hardcoded segment list.

**The regression tests that encode why each handler exists.** For §6, §7, §8:
assert through `_fetch_one` with a mocked client that **no GET is issued against
the publisher host** — no paywall scrape, no 403. For §10: assert the handler is
reached with no client interaction at all.

**`tests/test_springer.py`** must include a mocked Crossref response *with no
abstract*, asserting the S2 rung fires and its abstract reaches `card_summary`.
Given §8.1 this is the single most important test in the set.

**`tests/test_sciencedirect.py`** must cover the §8.5 miss: an
`alternative-id` query returning `total-results: 0` yields the URL-derived card,
`status="fetched"`, `http_status=None` — and, again, no GET against
`sciencedirect.com`.

**`tests/test_mitpress.py`** must cover both flag states:
`external_lookup_enabled=False` → slug card, **zero** requests;
`True` → `lookup_by_isbn` consulted via `asyncio.to_thread` and its synopsis on
the card. Plus a bad-checksum ISBN in the path being rejected rather than
written.

**`tests/test_domains.py`** gains one assertion per domain, including both APS
hosts and `linkinghub.elsevier.com`.

## 13. Open questions

- ~~**`direct.mit.edu` article paths carry no DOI** (§9). Reading `citation_doi`
  from the page is a scrape of a host that may or may not block us; the
  alternative is leaving that host to the generic path. Needs one probe against
  a real URL before committing either way — it is the only claim in this plan
  not verified against a live response.~~ **Settled 2026-09-03: it blocks us.**
  `direct.mit.edu/neco/article/9/8/1735/6109/…` and
  `direct.mit.edu/books/monograph/2313/…` both return `403` to a non-browser
  client, so the `citation_doi` route does not exist. Handled instead by
  `direct_mit.py`, which searches Crossref and verifies the hit against the
  URL's own volume/issue/page — see the status note at the top.
- **§3's default.** `doi_metadata_lookup: bool = True` is argued, not obvious.
  If the answer is "default off", §8.5's URL-derived card becomes the *normal*
  outcome for §6–§9 rather than the fallback, which changes what these handlers
  are for. Worth settling before writing §6.

## 14. Implementation order

`doi_meta.py` and `doi_from_path` first — everything else is a consumer.
Then §5 (`doi.org`), which exercises the ladder with the least URL parsing.
Then §6, §7, §8.1 in any order; they are near-identical once the spine exists.
Then §8.2 (ScienceDirect), which is the only one needing its own budget leg and
its own miss path. §9 and §10 are independent of all of the above and can be
built at any point — §10 needs nothing but `search_url.py` as a template, and is
the cheapest single improvement in the plan.
