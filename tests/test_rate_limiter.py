"""Rate-limiter tests — patch #14.

Verifies that:
  - Same-domain requests are spaced by at least ``1/rps`` seconds.
  - Concurrent same-domain waiters are spaced too, not released together.
  - Different-domain requests do not block each other.
"""
import asyncio
import time

import pytest

from pka.ingestion.fetch_base import _DomainRateLimiter


@pytest.mark.asyncio
async def test_same_domain_spaced_by_rps():
    """Two requests to the same domain should be at least ``1/rps`` apart."""
    limiter = _DomainRateLimiter(rps=2.0)   # 0.5s minimum gap
    t0 = time.monotonic()
    await limiter.wait("https://example.com/a")
    await limiter.wait("https://example.com/b")
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.45   # allow tiny clock skew


@pytest.mark.asyncio
async def test_different_domains_concurrent():
    """Different domains should not block each other."""
    limiter = _DomainRateLimiter(rps=1.0)
    t0 = time.monotonic()
    await asyncio.gather(
        limiter.wait("https://a.com/x"),
        limiter.wait("https://b.com/x"),
        limiter.wait("https://c.com/x"),
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 0.2   # all three should be near-instant


async def _wait_and_stamp(limiter: _DomainRateLimiter, url: str) -> float:
    await limiter.wait(url)
    return time.monotonic()


@pytest.mark.asyncio
async def test_concurrent_same_domain_requests_are_spaced():
    """Waiters that arrive together must still leave one gap apart.

    ``fetcher`` runs ``fetch_concurrency`` workers off one shared queue, so a
    run of same-domain URLs puts several of them in ``wait`` simultaneously.
    Each has to be given its own slot: reading a shared last-send time, sleeping
    the same interval and only then recording the send lets the whole group fire
    at once, which is the cap not binding at all.
    """
    limiter = _DomainRateLimiter(rps=10.0)   # 0.1s minimum gap
    stamps = sorted(await asyncio.gather(*(
        _wait_and_stamp(limiter, f"https://example.com/{i}") for i in range(4)
    )))
    gaps = [later - earlier for earlier, later in zip(stamps, stamps[1:], strict=False)]
    assert all(gap >= 0.08 for gap in gaps), gaps
