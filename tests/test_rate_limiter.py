"""Rate-limiter tests — patch #14.

Verifies that:
  - Same-domain requests are spaced by at least ``1/rps`` seconds.
  - Different-domain requests do not block each other.
"""
import asyncio
import time

import pytest

from pka.ingestion.fetcher import _DomainRateLimiter


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
