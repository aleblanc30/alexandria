"""Rate-limiter tests — patch #14.

Verifies that:
  - ``SlotScheduler`` spaces claims per key, deterministically, on a fake clock.
  - Same-domain requests are spaced by at least ``1/rps`` seconds.
  - Concurrent same-domain waiters are spaced too, not released together.
  - Different-domain requests do not block each other, sync or async.

The scheduler tests carry the load here: ``claim`` does no sleeping, so the
behaviour that actually matters is checkable with no elapsed time and no
tolerances. The limiter tests that follow are the thin remainder — that each
wrapper really does sleep for the delay it is handed.
"""
import asyncio
import time

import pytest

from pka.ingestion.rate_limit import (
    AsyncRateLimiter,
    SlotScheduler,
    SyncRateLimiter,
    domain_of,
)


class _FakeClock:
    """Monotonic clock the test moves by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TestSlotScheduler:
    def test_first_claim_is_immediate(self):
        sched = SlotScheduler(rps=1.0, clock=_FakeClock())
        assert sched.claim("example.com") == 0.0

    def test_claims_at_one_instant_are_spaced(self):
        """The bug this class exists to prevent.

        Four callers arriving together — the fetch pool on a run of same-domain
        URLs — must be handed increasing delays. Deriving each delay from a
        shared last-send time and recording the send only after sleeping gives
        all four the same answer, so they fire as one burst.
        """
        sched = SlotScheduler(rps=10.0, clock=_FakeClock())   # 0.1s gap
        delays = [sched.claim("example.com") for _ in range(4)]
        assert delays == pytest.approx([0.0, 0.1, 0.2, 0.3])

    def test_gap_already_elapsed_costs_nothing(self):
        clock = _FakeClock()
        sched = SlotScheduler(rps=2.0, clock=clock)   # 0.5s gap
        assert sched.claim("example.com") == 0.0
        clock.now += 5.0
        assert sched.claim("example.com") == 0.0

    def test_partial_gap_is_charged_for_the_remainder(self):
        clock = _FakeClock()
        sched = SlotScheduler(rps=2.0, clock=clock)   # 0.5s gap
        sched.claim("example.com")
        clock.now += 0.2
        assert sched.claim("example.com") == pytest.approx(0.3)

    def test_keys_are_independent(self):
        sched = SlotScheduler(rps=1.0, clock=_FakeClock())
        assert sched.claim("a.com") == 0.0
        assert sched.claim("b.com") == 0.0
        assert sched.claim("a.com") == pytest.approx(1.0)

    def test_rejects_non_positive_rps(self):
        with pytest.raises(ValueError):
            SlotScheduler(rps=0)


def test_domain_of_keys_on_netloc():
    assert domain_of("https://example.com/a?b=c") == "example.com"
    assert domain_of("https://example.com/a") == domain_of("https://example.com/b")
    assert domain_of("https://a.com/x") != domain_of("https://b.com/x")


@pytest.mark.asyncio
async def test_same_domain_spaced_by_rps():
    """Two requests to the same domain should be at least ``1/rps`` apart."""
    limiter = AsyncRateLimiter(rps=2.0)   # 0.5s minimum gap
    t0 = time.monotonic()
    await limiter.wait("https://example.com/a")
    await limiter.wait("https://example.com/b")
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.45   # allow tiny clock skew


@pytest.mark.asyncio
async def test_different_domains_concurrent():
    """Different domains should not block each other."""
    limiter = AsyncRateLimiter(rps=1.0)
    t0 = time.monotonic()
    await asyncio.gather(
        limiter.wait("https://a.com/x"),
        limiter.wait("https://b.com/x"),
        limiter.wait("https://c.com/x"),
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 0.2   # all three should be near-instant


async def _wait_and_stamp(limiter: AsyncRateLimiter, url: str) -> float:
    await limiter.wait(url)
    return time.monotonic()


@pytest.mark.asyncio
async def test_concurrent_same_domain_requests_are_spaced():
    """Waiters that arrive together must still leave one gap apart.

    ``fetcher`` runs ``fetch_concurrency`` workers off one shared queue, so a
    run of same-domain URLs puts several of them in ``wait`` simultaneously.
    """
    limiter = AsyncRateLimiter(rps=5.0)   # 0.2s minimum gap
    stamps = sorted(await asyncio.gather(*(
        _wait_and_stamp(limiter, f"https://example.com/{i}") for i in range(4)
    )))
    # Span rather than pairwise gaps: four waiters cover three gaps (0.6s), and
    # only the two endpoints carry timer jitter. Asserting each consecutive pair
    # compounds that jitter instead, which on a ~15ms-granularity timer is a
    # flaky test rather than a stricter one. The exact spacing is pinned without
    # any clock by ``TestSlotScheduler.test_claims_at_one_instant_are_spaced``.
    assert stamps[-1] - stamps[0] >= 0.45, stamps


class TestSyncRateLimiter:
    def test_same_domain_is_spaced(self):
        limiter = SyncRateLimiter(rps=10.0)   # 0.1s gap
        t0 = time.monotonic()
        limiter.wait("https://example.com/a")
        limiter.wait("https://example.com/b")
        assert time.monotonic() - t0 >= 0.08

    def test_different_domains_do_not_block_each_other(self):
        """The ``book_search`` case: the Brave rung must not wait out Google's gap."""
        limiter = SyncRateLimiter(rps=1.0)
        t0 = time.monotonic()
        limiter.wait("https://www.googleapis.com/books/v1/volumes")
        limiter.wait("https://api.search.brave.com/res/v1/web/search")
        assert time.monotonic() - t0 < 0.2
