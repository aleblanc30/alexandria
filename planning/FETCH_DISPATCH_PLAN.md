# Domain-aware fetch dispatch

**Status:** proposed, not implemented.
**Touches:** `pka/ingestion/fetcher.py`, `pka/ingestion/rate_limit.py`, `docs/ingestion-flows.md`.

## The problem

`_run_fetch_workers` (`pka/ingestion/fetcher.py:340`) runs `fetch_concurrency`
worker coroutines over one flat `asyncio.Queue`. A worker takes whatever is at
the head of the line, and the per-domain throttle is applied *after* that choice,
deep inside the fetch — `await _limiter.wait(url)` at `fetcher.py:162`.

So the wait happens in the wrong place. A worker that draws a URL whose domain
is a second away from its next slot spends that second asleep **while still
holding a worker slot**, with ready work from other domains sitting behind it in
the queue. On the shape a bookmark archive actually has — a few hundred URLs
from one busy host mixed into a long tail of one-off domains — every worker ends
up parked on that one host and the effective concurrency collapses to 1.

The limiter itself is correct; `SlotScheduler.claim` (`rate_limit.py:56`) spaces
same-domain sends properly and is well tested. The defect is that *dispatch*
ignores it: which URL a worker gets is decided without reference to which
domains can actually be sent to right now.

## Two constraints that shape any fix

Both were found by reading the call sites, and both rule out the obvious
"move throttling into the worker loop" version of this change.

**1. Per-site handlers claim their own slots, against their own hosts.**
`_fetch_one_impl` dispatches Wikipedia, arXiv and bioRxiv URLs to dedicated
handlers before reaching the plain GET, and those handlers call the limiter
themselves — `wikipedia.py:129`, `arxiv.py:147` (then `:173` for the PDF),
`biorxiv.py:105` (then `:137`), `wayback.py:33` and `:77`. A single arXiv item
therefore claims `export.arxiv.org` and then `arxiv.org`. A worker-level claim
cannot represent that, and stacking one on top would spend two slots per item
and halve the configured rate.

**2. The preprint handlers can decline.** `parse_arxiv_url` matching does not
guarantee `fetch_arxiv_paper` returns a result — on `None` control falls through
to the plain GET (`fetcher.py:145-155`). So the `_limiter.wait` at
`fetcher.py:162` cannot simply be deleted; some paths still need it.

The consequence: worker-level scheduling can only own **an item's first
request**, and only for items that take the plain-GET path. That is the right
scope anyway — it is where the pile-up happens.

## Proposed design

### `rate_limit.py` — read-only scheduling queries

`SlotScheduler` gains two methods that reserve nothing, and `AsyncRateLimiter`
exposes the scheduler for callers doing their own ordering:

```python
def now(self) -> float: ...
def next_slot(self, key: str) -> float:
    """When key's next send may go, as a clock time, reserving nothing."""
```

`next_slot` returns an **absolute** clock time, not a delay. This matters: the
queue compares readings taken at different moments, and two relative delays both
reading "0.1s away" — one taken at t=0, one at t=0.9 — compare equal while being
0.9s apart. Worth a test of its own.

### `fetcher.py` — `_DomainQueue`

Replaces the flat `asyncio.Queue`. Items are bucketed by domain; buckets are
ordered on a heap keyed by `next_slot`. `get()` returns the item plus the delay
the worker should sleep before its first request:

- a throttled item whose slot is open now, else
- an unthrottled item (needs no slot), else
- the throttled item whose slot opens soonest, with its delay.

**`get()` must be synchronous.** Choosing a bucket and claiming its slot happen
with no `await` between them, which is what stops two workers from both reading
a domain as ready and then stacking up behind each other. A peek-then-claim
split leaves that race open; this is the main reason the design is shaped this
way and should not be "simplified" later.

### `_throttle_key(url) -> str | None`

Answers "which domain's slot must a worker hold before starting this URL?"
`None` for anything that does not reach the plain GET: local paths and bad
schemes, skipped extensions, and the per-site handlers from constraint 1.

This mirrors the guards at the top of `_fetch_one_impl` and will drift from them
unless kept in step. The drift is not symmetric, and the docstring should say
so: a key where `_fetch_one_impl` would have returned early only wastes a slot,
while a *missing* key just means the item is not ordered — the `slot_held=False`
path still claims before sending, so the rate limit itself holds either way.

### `slot_held` plumbing

`_fetch_one` / `_fetch_one_impl` take a keyword-only `slot_held: bool = False`,
and the existing `_limiter.wait(url)` becomes conditional on it. True when the
queue claimed; false on the constraint-2 fall-through and for anything calling
`_fetch_one` outside the pool.

### Side effect worth keeping

The rate-limit wait moves *outside* the `asyncio.wait_for` in `_fetch_one`, so a
backed-up domain no longer eats the per-URL budget and turns a healthy site into
a spurious timeout. `_fetch_budget_seconds`'s docstring (`fetcher.py:101`)
currently claims the wait is included and must be corrected.

## Alternatives considered

**Re-queue with retries, as originally sketched.** Pushing a not-yet-ready URL
back onto the tail of a FIFO makes workers busy-spin: they re-draw the same item
and put it back until its slot opens. Ordering buckets by ready-time gets the
same outcome without the spin. (A min-heap keyed by ready-time *is* the
"put it back" idea, done once.)

**Peek without claiming, keep the claim where it is.** Least invasive, but racy:
between a worker peeking a domain as ready and the claim landing inside
`_fetch_one_impl` there are several awaits, so a whole tick's worth of workers
can select the same hot domain. Reintroduces the bug in narrower form.

**One in-flight request per domain.** A clean invariant, and at 1 rps arguably
the honest one — but it caps a single-domain queue at one request per
*response time* rather than per second, a real throughput regression on the
all-one-host case. Rejected.

## Testing

The queue takes a `SlotScheduler`, so injecting one with a fake clock makes all
of this testable with no elapsed time and no tolerances — the same approach
`TestSlotScheduler` already uses.

- `next_slot` reserves nothing; reflects a claim; orders busy after idle; and
  stays comparable across readings taken at different times.
- `_throttle_key`: host for a plain URL; `None` for local path, `file:` URL,
  skipped extension, and each of the three per-site handlers.
- `_DomainQueue`: a hot domain does not hold up an unrelated one; same-domain
  runs are spaced by the gap; slot-free work fills a gap instead of stalling;
  seeding picks up a domain already cooling from an earlier run (the limiter is
  module state); the queue drains each item exactly once.
- Through the pool: `slot_held=True` reaches `_fetch_one` for a plain URL, which
  is what prevents the double claim.

Existing test doubles for `_fetch_one` have a fixed `(client, doc_id, url)`
signature and will need widening to `**_`. That is a signature change, not a
behaviour change — no assertion should be weakened to accommodate it.

## Not solved by this

A queue made up *entirely* of one site's per-site handler — every URL a
Wikipedia one, say — still converges on that handler's own limiter, because
those items carry no throttle key and are never ordered. Rarer shape; call it
out in the class docstring rather than pretending otherwise.

## Doc sync

`docs/ingestion-flows.md:271-273` names `asyncio.Queue + Semaphore` and draws
`_limiter.wait(url)` between the pool and `_fetch_one`. It is already stale
today — there is no Semaphore anywhere in the code. Per the `CLAUDE.md` rule
this must be redrawn in the same commit; only the Firefox graph is affected
(the Reddit graph defers to it with "identical to Firefox").

## Note on formatting

`ruff check` is clean on this change, but `ruff format --check` wants to
reformat any file touched here — it also wants to reformat 182 of the repo's
203 files, so the formatter is not enforced at baseline. Leave formatting alone
rather than bury the diff in unrelated churn.
