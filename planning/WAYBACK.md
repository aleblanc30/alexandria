# Alexandria — Wayback Machine Submission

## Requirements Specification

Revision 2, 30 August 2026. Supersedes the draft of 28 August 2026.

---

## 1. Premise, Purpose and Scope

### 1.1 Premise

This subsystem is worth building **only if** Alexandria's entries are expected to
be cited or referenced by people other than its owner.

For any document already in the local corpus, the extracted text and — for
Zotero and Calibre items — the file itself are already held on disk. A Wayback
capture does not protect the owner's access to anything. Its single product is a
**publicly resolvable second address** for a URL, which is valuable when someone
else must be able to follow the reference after the original host is gone.

If the answer is no, the defensible scope collapses to fragile non-scholarly web
pages whose loss would break the *local* record — and even then the local
extracted text already survives. The rest of this document assumes the answer is
yes. It is stated here rather than deferred because it determines the size of the
subsystem, not merely its behaviour.

### 1.2 Purpose

Submit selected Alexandria document URLs to the Internet Archive's Save Page Now
service (SPN2) so each acquires a durable third-party snapshot, and record the
resulting snapshot address against the document.

### 1.3 In scope

- Deciding which URLs are worth submitting.
- Submitting them within the service's rate and quota constraints.
- Recording the outcome against the document row.

### 1.4 Out of scope

- Local corpus ingestion and text extraction — the existing pipeline.
- **Dead-link detection and recovery.** See §3.4: no such subsystem exists today,
  and this one does not create one. A URL skipped at Gate 3 (coverage) is never
  probed, so this subsystem cannot be relied on to notice rot.
- Archival to services other than the Internet Archive.
- Replacing or extending the Wayback *read* fallback in
  [`pka/ingestion/wayback.py`](pka/ingestion/wayback.py).

### 1.5 Conventions

Identifiers use the prefixes `FR` (functional), `NF` (non-functional) and `CFG`
(configuration). SHALL, SHOULD and MAY carry their conventional specification
meaning.

---

## 2. Definitions

**Bookmark date.** `documents.date_added` (unix ts from the source, recording
when the user saved it). Where NULL, `documents.ingested_at`.

**Ingestion date.** `documents.ingested_at` — when Alexandria first indexed the
row. This, not the bookmark date, is the clock for submission deferral (FR-10.2).

**Covered.** The Wayback CDX index holds at least one capture of the URL with
status 200, a recorded length above the floor (FR-16), and a capture timestamp
later than the bookmark date.

**Submittable.** A document row that is in a configured source (FR-1), whose
`url_or_path` is an `http(s)` URL, and which no static exclusion rule matches.

**Live snapshot address.** `documents.snapshot_url` — a durable public address
for a page that is *still alive*. Distinct from `documents.archive_url`; see
FR-4, which is a correctness requirement, not a naming preference.

---

## 3. Integration With Existing Code

This section is normative. The 28 August draft specified a standalone subsystem
and consequently duplicated or contradicted machinery that already ships.

### 3.1 Reuse

**FR-1.** The subsystem SHALL operate over document rows whose `source` is in a
configured set (CFG `wayback_sources`), defaulting to `firefox,reddit`.

Rationale for the default, per source:

| Source | Default | Why |
|---|---|---|
| `firefox` | in | Ordinary web bookmarks; the core case. |
| `reddit` | in | See §5.2 for the comment/post/link-target split. |
| `youtube` | out | A watch page captures as a shell; the video does not capture at all. |
| `zotero` | out | `url_or_path` frequently holds a bare DOI, not a URL ([`urls.ts:29`](frontend/src/lib/urls.ts:29)); those that are URLs are mostly publisher landing pages (§5.3). |
| `calibre`, `image` | out | Local files; no URL exists. |

**FR-2.** Static exclusion of non-web schemes and local paths SHALL reuse
[`bookmark_url_unfetchable_reason()`](pka/ingestion/fetcher.py:72) rather than
reimplementing scheme, `file:`, UNC and drive-letter handling.

**FR-3.** Outbound HTTP SHALL go through the existing `httpx` client conventions
in [`pka/ingestion/fetch_base.py`](pka/ingestion/fetch_base.py), reusing
`_http_timeout()`, the `cfg.fetch_user_agent` string, and `_limiter` for probes
against origin servers. A new User-Agent SHALL NOT be introduced.

### 3.2 `archive_url` must not be overloaded

**FR-4.** The subsystem SHALL NOT write to `documents.archive_url`. It SHALL
write to new columns `snapshot_url` and `snapshot_timestamp` (FR-22).

This is a hard requirement because
[`resolveOpenUrl()`](frontend/src/lib/urls.ts:36) returns `archive_url` **in
preference to** the live URL whenever it is set:

```ts
if (archiveUrl && isHttpUrl(archiveUrl)) return archiveUrl.trim()
return resolveHttpUrl(source, urlOrPath)
```

That is correct today, because `archive_url` is populated only when the original
returned 404 and the local text came from a snapshot — there, the snapshot *is*
the best available link. Writing a fresh snapshot URL to that column for every
successfully submitted bookmark would make Browse silently serve a Wayback copy
of pages that are alive and current, degrading the reading experience of the
whole archive as a side effect of preserving it.

**FR-5.** The two columns SHALL carry these distinct meanings, and the invariant
SHALL be stated in [`pka/db/schema.py`](pka/db/schema.py:21) alongside both:

| Column | Meaning | Overrides the live link? |
|---|---|---|
| `archive_url` | The locally held text was extracted from this snapshot because the origin was gone. | Yes |
| `snapshot_url` | A durable second address exists here; the origin was alive when it was made. | **No** |

**FR-6.** The frontend SHALL surface `snapshot_url` as a secondary affordance
(an "archived copy" link or icon on the document detail panel), never as the
primary open target. `resolveOpenUrl()` SHALL be left unchanged.

### 3.3 URL normalisation

**FR-7.** A normalisation helper SHALL be added to `pka/` (there is none today;
[`frontend/src/lib/urls.ts`](frontend/src/lib/urls.ts) does scheme and path
classification only, no normalisation).

**FR-8.** Normalisation SHALL lowercase scheme and host, remove default ports,
remove the fragment, and strip a configured tracking-parameter list (`utm_*`,
`fbclid`, `gclid`, `mc_eid`, `igshid`, `si` on `youtu.be`, plus CFG extensions).

**FR-8.1.** The normalised form SHALL be used **only** for in-run deduplication —
grouping rows that would otherwise produce duplicate submissions. The URL sent to
SPN and queried against CDX SHALL be `documents.url_or_path` as stored. The
Wayback Machine applies its own SURT canonicalisation server-side; local
re-canonicalisation risks producing a key the archive does not match.

**FR-8.2.** Where several document rows share a normalised URL, one submission
SHALL be made, using the earliest bookmark date, and the resulting
`snapshot_url` / `snapshot_timestamp` SHALL be written to **every** row in the
group. Documents are keyed `(source, source_id)`
([`schema.py:31`](pka/db/schema.py:31)) with no URL uniqueness constraint;
grouping is therefore a per-run in-memory operation and introduces no second
identity space in the database.

### 3.4 There is no dead-link recovery path

**FR-9.** The subsystem SHALL NOT claim to hand rotten URLs to a recovery path.

[`pka/ingestion/wayback.py`](pka/ingestion/wayback.py) is a *fallback inside a
fetch*: on HTTP 404 during the Firefox phase-2 fetch it queries
`archive.org/wayback/available` and substitutes the snapshot's text. It persists
no liveness verdict and consumes none. A probe result indicating rot SHALL be
written to the log table (FR-13) and otherwise no action taken.

### 3.5 Scheduling

**FR-10.** Alexandria has no scheduler. Background work runs as
`fastapi.BackgroundTasks` on a worker thread
([`runs.py:53`](pka/api/routers/runs.py:53)); `alexandria dev` is the API server,
not a daemon. The subsystem SHALL therefore have exactly two entry points, both
calling one implementation:

- **FR-10.1 — Sweep.** `alexandria wayback [--source S] [--since D] [--limit N]
  [--dry-run]`, a CLI subcommand processing the collection in bulk, safe to
  interrupt and restart at any point.
- **FR-10.2 — Drain.** A tail step in each sync run that processes documents
  whose deferral (FR-16) has elapsed since the previous run.

There SHALL be no timer, queue daemon or deferred-execution mechanism. "Due"
work is discovered by query at the start of a run, never scheduled for a future
wake-up.

---

## 4. Network Policy Compliance

**NF-1.** This is the first outbound path in Alexandria that **publishes**. It
discloses the URL list to a third party permanently and publicly, and it causes
third-party crawls that appear in the origin sites' access logs. That is a larger
and less reversible disclosure than the "derived identifiers" category, and
unlike inference calls it cannot be undone by changing a setting later.

**NF-2.** `DESIGN.md` §1.1 SHALL gain a fourth category row before this ships:

| Category | Settings | What is sent |
|---|---|---|
| Publication | `wayback_submit_enabled` | **URLs, publicly and permanently.** Discloses collection membership to a third party and triggers third-party crawls of the origin, visible in its logs. Not revocable. |

**NF-3.** The subsystem SHALL be gated by a single named setting,
`wayback_submit_enabled`, **defaulting to `False`**, registered in the
`_parse_bool` validator list ([`config.py:420`](pka/config.py:420)).

**NF-4.** No other flag SHALL enable it. In particular `fetch_wayback_fallback`
(default `True`) governs the *read* fallback only and SHALL NOT imply submission
— that would be exactly the implicit escalation `DESIGN.md` §1.1 forbids.

**NF-5.** Credentials SHALL be read from `.secrets` as
`SECRET_ALEXANDRIA_ARCHIVE_ORG_KEY` and `SECRET_ALEXANDRIA_ARCHIVE_ORG_SECRET`
([`config.py:27`](pka/config.py:27)), never from `.env`, the repository, or the
database. Absent credentials SHALL disable the subsystem with a logged warning,
not an error.

**NF-6.** With no `.env` and no `.secrets`, a fresh checkout SHALL make no
archive.org call from this subsystem. AC-1 tests this.

---

## 5. Eligibility

Candidates pass four gates in order: cheapest and most decisive first, so quota
and origin requests are not spent on URLs a later gate would reject.

Gates 1 and 2 are **recomputed every run and never persisted**. They are
functions of mutable configuration; storing their verdicts would guarantee drift
the moment a rule is edited.

### 5.1 Gate 1 — Scope and normalisation

**FR-11.** Rows outside `wayback_sources`, rows whose `url_or_path` is not
`http(s)` (FR-2), and rows already in a terminal persisted state (FR-14) are
dropped. Survivors are normalised (FR-8) and grouped (FR-8.2).

### 5.2 Gate 2 — Static exclusion

**FR-12.** URLs matching any rule below SHALL be excluded before any network
call, with the matching rule recorded as the run's reason code:

| Class | Rule | Rationale |
|---|---|---|
| Private hosts | Loopback, RFC 1918, `.local`, `.internal`, configured self-hosted domains | Unreachable from the archive's crawlers, and undesirable to disclose |
| Reddit comment permalinks | Path matching `/r/<sub>/comments/<id>/<slug>/<comment_id>` | Comment threads capture poorly; the text is already in `reddit_items.body` ([`schema.py:99`](pka/db/schema.py:99)) |
| Authentication-gated services | Configured host list (webmail, private document hosts, workspace tools) | Capture would record a login page |
| Signed or session-scoped URLs | Query contains `token`, `sig`, `signature`, `Expires`, `X-Amz-*`, or a configured pattern | The credential expires, the capture is worthless, and the credential is disclosed |
| Excluded hosts | Host in `wayback_excluded_hosts` (§5.3) | Independently preserved, or systematically uncapturable |
| Learned exclusions | Host over the failure threshold (FR-13) | Prior terminal SPN failures on that host |

**FR-12.1.** The rule set SHALL be configuration, editable without
redeployment, each rule individually disableable.

**FR-12.2.** Reddit **post** permalinks and link-post targets are not excluded by
the Reddit rule; only comment permalinks match. Note that the schema already
settles the "two URLs" question: a link post's `documents.url_or_path` **is** the
external target, and the thread URL lives separately in `reddit_items.permalink`
([`schema.py:97`](pka/db/schema.py:97)). Only `url_or_path` is submitted, so the
external destination — the content actually at risk — is what gets captured, and
no additional policy is required.

**FR-13.** Learned exclusion SHALL be **derived, not stored**: a host is excluded
when `wayback_log` (FR-15) holds at least `wayback_blocklist_threshold` terminal
failures of the same error class for that host within the last
`wayback_blocklist_window_days`. This needs no blocklist table, no TTL field and
no expiry job — the window is a query predicate, and an archive-side restriction
that lifts stops matching on its own.

### 5.3 Excluded hosts, including scholarly ones

**FR-14.** There SHALL be **one** flat, configurable host exclusion list
(`wayback_excluded_hosts`), seeded with the preprint servers and repositories
that are independently preserved and least rewarding to capture: `arxiv.org`,
`biorxiv.org`, `medrxiv.org`, `ncbi.nlm.nih.gov`, `pubmed.ncbi.nlm.nih.gov`,
`zenodo.org`, `hal.science`, plus large commercial publisher domains.

**FR-14.1.** There SHALL NOT be a scholarly host taxonomy. The 28 August draft's
three classes collapse under their own rules: `fragile_scholarly` membership was
defined as "any other host serving a PDF or a document identified as a paper",
while classification was required to use host and path alone with no network
call. Most fragile scholarly URLs are neither `.pdf` nor carry a recorded DOI, so
they would fall through to `not_scholarly` — which is admitted anyway. What
remains after the collapse is two knobs:

- a configured list of excluded hosts (FR-14), and
- a raised size ceiling when the URL path ends in `.pdf` (CFG-1).

Anything more elaborate implies a discrimination the implementation will not
have.

**FR-14.2.** Membership determines submission policy only. It SHALL have no
effect on local corpus ingestion.

### 5.4 Gate 3 — Coverage

**FR-15.** Coverage SHALL be determined by a CDX Server API query with exact
matching, filtered to `statuscode:200`, returning the most recent capture's
timestamp and length. The digest SHALL NOT be requested; nothing consumes it
until drift detection exists (§9).

**FR-15.1.** The query SHOULD carry the archive.org credentials. The claim that
the authenticated allowance is materially larger SHALL be verified by measurement
before the first live sweep (NF-9), not assumed.

**FR-16.** A URL is covered when a returned capture timestamp is later than the
bookmark date **and** the recorded length exceeds `wayback_min_capture_bytes` — a
small 200 response is usually a soft error or interstitial rather than the
document.

**FR-17.** The coverage result SHALL be cached in `snapshot_checked_at` and SHALL
NOT be re-queried within `wayback_coverage_freshness_days`. `covered` is a
**cache entry, not a terminal state** (FR-23); after the freshness window
it is re-evaluated.

### 5.5 Gate 4 — Liveness

**FR-18.** Liveness SHALL be determined by a **range-limited GET**
(`Range: bytes=0-2047`) following up to `wayback_redirect_limit` redirects, with
`_http_timeout()` from `fetch_base`.

`HEAD` SHALL NOT be used. Enough origins return 403/404/405 to `HEAD` while
serving `GET` correctly that a HEAD-first probe manufactures false rot
classifications, and the fallback-on-405/501 rule in the previous draft does not
cover the 403/404 cases. A range GET is one code path instead of two, is more
accurate, and is the only form that can supply the response body FR-20's
heuristic needs.

**FR-19.** Responses SHALL be classified:

| Response | Classification |
|---|---|
| 2xx | Live |
| 3xx resolving to 2xx | Live; final URL recorded in the log |
| 401, 403 | Gated — excluded (see §9) |
| 404, 410 | Rotten — logged, not submitted, no further action (FR-9) |
| 429, 5xx | Indeterminate — deferred to the next run |
| DNS, connection, or TLS failure | Rotten after `wayback_probe_retries` confirming attempts |

**FR-20.** The subsystem SHOULD apply soft-404 heuristics: a 200 whose final URL
is the site root after a redirect from a deep path, or whose body matches a
configured parking or content-removed signature. A match SHALL defer and flag for
review, never classify terminally — the heuristic is unreliable.

**FR-21.** Where the response reports a size (`Content-Range` total or
`Content-Length`), it SHALL be recorded, and URLs above the applicable ceiling
(CFG-1) SHALL be deferred pending an explicit decision. The ceiling is higher for
`.pdf` paths.

**FR-21.1.** This is a coarse guard, not an accounting mechanism. The figure is
absent on chunked responses, wrong across redirect chains, and — decisively —
bounds only the main document, while the quota is consumed by the archive's fetch
of the page *plus its subresources*, which the probe never sees. NF-8 is written
accordingly.

---

## 6. Persisted State

**FR-22.** State SHALL live in new nullable columns on `documents`, added
idempotently in `init_db()` following the existing
`PRAGMA table_info` / `ADD COLUMN` idiom
([`queries.py:54`](pka/db/queries.py:54)):

| Column | Type | Meaning |
|---|---|---|
| `snapshot_state` | TEXT | NULL, `covered`, `submitted`, `confirmed`, `parked` |
| `snapshot_url` | TEXT | Durable public address (never overrides the live link — FR-4) |
| `snapshot_timestamp` | TEXT | Wayback 14-digit capture stamp |
| `snapshot_checked_at` | INTEGER | Unix ts of the last CDX query |
| `snapshot_job_id` | TEXT | SPN job id of the most recent submission |

Putting state on `documents` rather than in a keyed submission table means
`purge-source` deletes it for free and no row can outlive its document. A
separate table keyed by normalised URL would create rows with no owner and would
need adding to `_CHILD_TABLES`
([`purge_source.py:34`](pka/cli/purge_source.py:34)) — the previous draft had no
deletion story at all.

**FR-23.** State values:

| Value | Terminal? | Meaning |
|---|---|---|
| NULL | — | Never evaluated, or last evaluation reached no conclusion |
| `covered` | **No** | A pre-existing capture was found; re-evaluated after the freshness window (FR-17) |
| `submitted` | No | A POST was issued; `snapshot_job_id` set; awaiting poll or reconciliation |
| `confirmed` | Yes | This subsystem produced a capture; URL and timestamp stored |
| `parked` | Yes until operator action | Terminal SPN error, or transient attempts exhausted |

**FR-23.1.** There SHALL be no `checking`, `eligible`, `excluded` or `rotten`
persisted state. Gates 1–4 have no side effects worth resuming, so persisting
entry into them only produces stuck rows; and `excluded` is a function of
editable config, so storing it guarantees the stored verdict and the live rules
disagree after the first edit.

**FR-24.** A `wayback_log` table SHALL record attempts, mirroring the existing
`fetch_log` shape ([`schema.py:72`](pka/db/schema.py:72)) and added to
`_CHILD_TABLES` in `purge_source.py`:

```
wayback_log(id, document_id FK, timestamp, event, job_id, http_status, detail)
```

It is the evidence store for FR-13's derived blocklist and for FR-33's audit
requirement.

**FR-25.** The transition to `submitted`, with `snapshot_job_id`, SHALL be
persisted **before** the POST is issued, so a crash mid-call cannot produce a
silent duplicate on restart. This is the only write-ahead requirement; it is
sufficient because every other recovery route is idempotent.

**FR-26.** Restart recovery SHALL consist of one query for rows in
`submitted`, resuming their poll. Rows in `submitted` past the poll timeout SHALL
be reconciled by coverage check, not resubmitted, and the reconciliation SHALL
NOT run until at least `wayback_reconcile_delay_hours` after submission —
otherwise index lag (FR-30) will mark real successes unconfirmed. Reconciliation
that still finds nothing leaves the row in `submitted` for the next run; it does
not park it.

---

## 7. Submission

**FR-27.** Submission SHALL `POST` to `https://web.archive.org/save`,
authenticated with archive.org S3-style keys in the `Authorization` header,
requesting JSON.

**FR-28.** The request SHALL set `capture_outlinks=0` — outlink capture
multiplies submitted volume by an unbounded factor and can overwhelm small
origins — and `capture_screenshot=0`. `capture_all` is left at its default so
error pages are not saved; liveness was established at Gate 4.

**FR-29.** `if_not_archived_within` SHALL be set to a **fixed short window**
(CFG-1, default `30d`), as a race guard against a capture made between Gate 3 and
the POST.

**FR-29.1.** It SHALL NOT be derived from the bookmark date. The parameter is a
window relative to *now*, not an absolute date: deriving it from a 2016 bookmark
yields a ten-year window, meaning "skip if any capture exists in the last
decade", which silently suppresses nearly every submission the subsystem exists
to make while reporting success. The 28 August draft specified exactly this.

**FR-29.2.** The parameter set SHALL be re-read against the current SPN2 public
API documentation at implementation time; it is a living draft and has changed
across revisions.

**FR-30.** Job status SHALL be polled at
`https://web.archive.org/save/status/<job_id>` from `wayback_poll_initial`,
growing geometrically to `wayback_poll_ceiling`, abandoned after
`wayback_poll_timeout`. Abandonment leaves the row in `submitted` for FR-26
reconciliation; it is not a failure.

**FR-31.** Success SHALL NOT be verified by an immediate CDX query. A new capture
may take hours or days to reach the long-term index and may appear and disappear
during index migration. The job status response is authoritative. (FR-26's
reconciliation is the same query at a much longer delay, which is why FR-26
carries a minimum.)

**FR-32.** On success, `snapshot_url`, `snapshot_timestamp` and state
`confirmed` SHALL be written to every row in the FR-8.2 group.

---

## 8. Rate, Quota and Failure

**NF-7.** All published SPN2 rate figures SHALL be treated as **unverified**.
The values circulating — 15 URLs/minute anonymous, 60 parallel jobs, a large
authenticated CDX allowance — are undocumented, have changed, and are not
suitable as requirements. CFG-1's defaults are deliberately below any of them and
exist to be corrected by measurement, not to encode a known limit.

**NF-8.** Rate control SHALL be an adaptive token bucket: start at
`wayback_rate_per_minute`, increase gradually after a sustained run of successes,
**halve** on any 429 or explicit rate-limit response, with a configured floor.
Concurrent in-flight jobs SHALL be capped by `wayback_max_concurrent_jobs`.

**NF-8.1.** There SHALL be no per-origin pacing requirement for submissions. Every
submission request goes to `web.archive.org`; the origin fetch is performed by
the archive's crawler on its own schedule and cannot be paced from here. The
previous draft's NF-5 was not implementable. What remains is a scheduling hint:
the sweep SHOULD interleave hosts rather than processing a domain contiguously.
The existing `_limiter` ([`fetch_base.py:42`](pka/ingestion/fetch_base.py:42))
continues to pace this subsystem's *own* Gate 4 probes against origins, which is
a different and real constraint.

**NF-9.** Before the first live sweep, a **calibration run** SHALL submit a small
configured number of URLs and record observed rate limits, error strings and, if
determinable, quota responses. CFG-1 defaults SHALL be revised from its output.
The SPN user-status endpoint SHOULD be queried where available; its shape is to
be confirmed at implementation time.

**NF-10.** Cumulative submitted volume SHALL be tracked against
`wayback_daily_byte_budget` and submission suspended on approach. Per FR-21.1
this is an estimate with unbounded error and functions as a circuit breaker, not
an accounting record; the summary (FR-33) SHALL label it as estimated.

**NF-11.** A sweep SHOULD be confinable to a configured time window so a
multi-day run can be held to off-peak hours. This is a CLI-invoked constraint,
not a scheduler (FR-10).

**FR-33.** SPN status errors SHALL be classified terminal or transient by a
**configuration table**, so new error strings need no code change.

- Terminal: robots exclusion, site blocked by takedown, capture refused for the
  URL, unsupported content type. Never retried automatically; row → `parked`.
- Transient: proxy errors, job timeouts, rate limiting, service unavailable.
  Retried with exponential backoff and full jitter to
  `wayback_retry_ceiling` attempts, then row → `parked`.

**FR-34.** `parked` rows SHALL be listable (`alexandria wayback --parked`) and
resettable to NULL (`alexandria wayback --unpark <id>|--unpark-all`). A state no
operator interface can leave is a leak; the previous draft parked rows and never
released them.

**FR-35.** No failure of this subsystem SHALL affect the local corpus.
Submission is advisory; ingestion SHALL succeed with archive.org unreachable, and
the drain step (FR-10.2) SHALL never fail a sync run.

---

## 9. Configuration

**CFG-1.** All of the following SHALL be settable via the `ALEXANDRIA_` env
prefix or `.env`, declared in [`pka/config.py`](pka/config.py) beside the
existing fetch settings. Credentials are `.secrets` only (NF-5).

| Setting | Default | Note |
|---|---|---|
| `wayback_submit_enabled` | **`False`** | Master flag; NF-3 |
| `wayback_sources` | `firefox,reddit` | FR-1 |
| `wayback_defer_hours` | `24` | From `ingested_at`; FR-10.2 |
| `wayback_coverage_freshness_days` | `30` | FR-17 |
| `wayback_min_capture_bytes` | `2048` | FR-16 |
| `wayback_redirect_limit` | `5` | FR-18 |
| `wayback_probe_retries` | `2` | FR-19 |
| `wayback_size_ceiling_bytes` | `5_000_000` | FR-21 |
| `wayback_size_ceiling_pdf_bytes` | `50_000_000` | FR-21 |
| `wayback_rate_per_minute` | `6` | Provisional; NF-7, NF-9 |
| `wayback_max_concurrent_jobs` | `6` | Provisional; NF-7 |
| `wayback_daily_byte_budget` | `2_000_000_000` | Estimated; NF-10 |
| `wayback_if_not_archived_within` | `"30d"` | Fixed window; FR-29 |
| `wayback_poll_initial` / `_ceiling` / `_timeout` | `5s` / `60s` / `15min` | FR-30 |
| `wayback_reconcile_delay_hours` | `48` | FR-26 |
| `wayback_retry_ceiling` | `5` | FR-33 |
| `wayback_blocklist_threshold` / `_window_days` | `5` / `30` | FR-13 |
| `wayback_excluded_hosts` | Per FR-14 | |
| `wayback_exclusion_rules` | Per FR-12 | |
| `wayback_log_retention_days` | `365` | FR-36 |

**CFG-2.** `wayback_submit_enabled` SHALL be added to the `_parse_bool`
validator list ([`config.py:420`](pka/config.py:420)), and every new setting
documented in `.env.example` — with the credentials shown as `.secrets` keys, not
`.env` keys.

---

## 10. Observability

**FR-36.** Each run SHALL emit a summary: counts by outcome and reason code,
**broken down by source**, estimated bytes submitted, mean and tail submission
latency, and hosts newly over the FR-13 threshold. `wayback_log` rows SHALL be
retained for `wayback_log_retention_days`.

**FR-37.** A dry-run mode (`--dry-run`) SHALL execute Gates 1–3 and report the
outcome each URL would receive, issuing no submission. It MAY skip Gate 4 (which
touches origin servers) under `--no-probe`.

**FR-38.** The dry run SHALL report per-source counts of excluded, covered and
eligible URLs, so the byte budget can be estimated before the first live run.
This is the only way to learn the real size of the job: the collection's URL
count is not the submission count.

---

## 11. Acceptance Criteria

All criteria SHALL be verifiable under `tests/conftest.py`, which redirects data
paths to `tmp_path` and mocks HTTP; per `.claude/rules/tests.md` a test that
reaches the network is broken, not thorough. Criteria are therefore stated as
properties of the request stream and of stored values, never of live service
behaviour.

| # | Criterion |
|---|---|
| **AC-1** | With no `.env` and no `.secrets`, a full run issues **zero** requests to any archive.org host. (Project invariant, NF-6 — absent from the previous draft.) |
| **AC-2** | A dry run over the whole collection produces an outcome for every in-scope row, with per-outcome counts summing to the in-scope row count, reported per source. |
| **AC-3** | `resolveOpenUrl()` returns the live URL for a document whose `snapshot_url` is set and whose `archive_url` is NULL. (FR-4 — the regression this revision exists to prevent.) |
| **AC-4** | Killing the run at the single injected fault seam — between the FR-25 state write and the POST — and restarting produces no second POST for any URL. The seam is a design requirement, not just a test fixture: one function, one await point, patchable from a test. |
| **AC-5** | No Reddit comment permalink is submitted; a link post submits its external target and not its thread URL. |
| **AC-6** | No URL with a post-bookmark-date 200 capture above the length floor is submitted. |
| **AC-7** | Against a mock returning 429, the request stream shows the bucket halving and no request exceeding the configured ceiling. |
| **AC-8** | `if_not_archived_within` is the configured fixed window on every request, and is never a function of `date_added`. (FR-29.1.) |
| **AC-9** | Every row reaching `confirmed` has non-NULL `snapshot_url` and `snapshot_timestamp` matching the mocked job response. |
| **AC-10** | `alexandria purge-source firefox` leaves no `wayback_log` row and no document-level snapshot state for that source. |
| **AC-11** | Ingestion completes normally, and the sync run reports success, with every archive.org endpoint returning connection errors. |
| **AC-12** | `init_db()` run twice against a pre-existing archive adds the columns once and errors on neither pass. |
| **AC-13** | A host with `wayback_blocklist_threshold` logged terminal failures inside the window is excluded; the same host with those rows aged past the window is not. |
| **AC-14** | A `parked` row is listed by `--parked` and returns to NULL after `--unpark`. |

---

## 12. Documentation Obligations

**FR-39.** `DESIGN.md` §1.1 SHALL gain the Publication category row (NF-2), and
§1's source list SHALL note which sources participate.

**FR-40.** If the drain step (FR-10.2) becomes a named phase in the progress
tracker, [`docs/ingestion-flows.md`](docs/ingestion-flows.md) SHALL be updated
**in the same commit**, per `CLAUDE.md`: a new outbound (red) node on every
participating source's graph, and a row in the shared-machinery matrix. If it
runs as an untracked tail step outside `STANDARD_PHASES`
([`progress/state.py:18`](pka/ingestion/progress/state.py:18)), the graphs are
unaffected and this obligation does not apply — which is a further argument for
keeping it outside the phase machinery.

**FR-41.** `README.md` SHALL document the flag, the credentials, and the fact
that enabling it publishes URLs to a third party.

---

## 13. Open Choices

Reduced from five to two. The rest were settled above: link posts by the schema
(FR-12.2), major publishers and preprints by the flat host list (FR-14), and the
citation premise by promotion to §1.1.

**Gated URLs (401/403).** Currently excluded. Excluding them means a paywalled
article that later vanishes leaves no trace; submitting them archives a paywall
page, which records that the URL existed and what it claimed to be. A middle
position submits only soft paywalls — a 200 with truncated content — and excludes
hard 401s. Deciding this changes FR-19 and AC-6 only.

**Freshness rule.** FR-16 marks a 2016 bookmark covered if a 2017 snapshot
exists, permanently. That is correct if the intent is to record the page as it
was when bookmarked, and wrong if the intent is durable access to current
content. Implementing the alternative — resubmit on content drift — requires
storing the CDX digest, which FR-15 currently declines to request precisely
because nothing consumes it. Adding drift detection is therefore a coherent
increment: request the digest, add a `snapshot_digest` column, compare against
the locally extracted text's hash. It is not free, and it is not needed for the
§1.1 premise.

---

## 14. Changes From the 28 August Draft

| Area | Change |
|---|---|
| `archive_url` | **No longer written.** Reusing it would have made Browse serve Wayback copies of live pages for the whole collection (FR-4). |
| Recovery path | Claim removed. No dead-link recovery subsystem exists to hand rot to (FR-9). |
| Scheduling | Deferred queue replaced by CLI sweep + per-sync drain. There is no scheduler in this project (FR-10). |
| Per-host pacing | NF-5 deleted; it was not implementable against a single-host API (NF-8.1). |
| `if_not_archived_within` | Fixed window instead of bookmark-date derivation, which would have silently suppressed most submissions (FR-29.1). |
| FR-2 / FR-3 conflict | Verdict reuse dropped; the 24 h deferral exists to distrust exactly that verdict. |
| `covered` / `excluded` terminality | `covered` is a cache entry with a TTL; `excluded` is no longer persisted at all (FR-23.1). |
| State machine | Six states to four; `checking` removed; three overlapping crash-safety mechanisms reduced to one write-ahead point (FR-25). |
| Storage | Submission table replaced by columns on `documents` plus a `fetch_log`-shaped log; purge works for free (FR-22). |
| Probe | HEAD-first replaced by range GET (FR-18). |
| Scholarly taxonomy | Three classes replaced by one host list plus a PDF size ceiling (FR-14.1). |
| Byte budget | Demoted to a circuit breaker with stated estimation error (FR-21.1, NF-10). |
| Rate figures | Restated as unverified, with a calibration run required first (NF-7, NF-9). |
| Network policy | Master flag defaulting off, `.secrets` credentials, and a new §1.1 "Publication" category naming the disclosure (NF-1..NF-6). |
| Scope | Named source set with per-source rationale, instead of "bookmark URLs" (FR-1). |
| CDX digest | No longer requested, since nothing consumed it (FR-15); reinstating it is the drift-detection increment (§13). |
| Parked rows | Given an operator interface (FR-34). |
| Acceptance | Rewritten to be mock-verifiable; added the fresh-checkout, purge, migration-idempotency, `if_not_archived_within` and unpark criteria; specified the fault seam AC-4 needs. |
| Premise | The citation question moved from §11 to §1.1, because it decides whether to build this at all. |

---

## 15. References

[1] Eve, M. P. (2024). Digital Scholarly Journals Are Poorly Preserved: A Study
of 7 Million Articles. *Journal of Librarianship and Scholarly Communication*,
12(1), eP16288. Cited in the previous draft to argue for submitting publisher
landing pages. It is not load-bearing here: it measures presence in preservation
archives rather than current accessibility, and excludes institutional
repositories. FR-14 excludes large publishers on the simpler ground that a
capture would record a paywall.
