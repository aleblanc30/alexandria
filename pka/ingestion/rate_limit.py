"""Per-domain rate limiting, shared by the async fetch path and the sync
enrichment lookups.

Lives on its own rather than in ``fetch_base`` so that ``openlibrary`` and
``book_search`` can throttle without importing the fetch stack: a twenty-line
utility should not drag ``book_extractor`` and the PDF machinery into the
retrieval-enrichment path (``DESIGN.md`` §3.2).

The split here is deliberate. :class:`SlotScheduler` holds the whole of the
tricky part — the lock, the clock and the arithmetic — and does no sleeping, so
it is exhaustively testable with a fake clock and no elapsed time. The two
limiters are the thin remainder: one awaits, one blocks, and neither knows
anything else. Duplicating the scheduler instead, once per concurrency model,
is what previously let the async copy drift into not limiting at all while the
sync copy stayed correct by accident.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from urllib.parse import urlparse


def domain_of(url: str) -> str:
    """The netloc a URL is rate-limited against."""
    return urlparse(url).netloc


class SlotScheduler:
    """Hands out send slots spaced ``1/rps`` apart, independently per key.

    ``claim`` reserves the caller's slot *before* releasing the lock and returns
    how long to wait for it. The reservation is the point: deriving a delay from
    a shared last-send time and recording the send only after sleeping lets
    every caller that arrives within one gap read the same value, wait the same
    interval and then fire together — a burst the size of the caller pool, and
    no effective limit.

    ``threading.Lock`` rather than ``asyncio.Lock`` so one scheduler serves both
    threads and coroutines, and so an async user survives ``asyncio.run()``
    being called more than once in a process. Nothing sleeps or does I/O while
    it is held.
    """

    def __init__(self, rps: float = 1.0, *, clock: Callable[[], float] = time.monotonic) -> None:
        if rps <= 0:
            raise ValueError(f"rps must be positive, got {rps!r}")
        self._gap = 1.0 / rps
        self._clock = clock
        # Earliest time at which the next send for a key may go.
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def claim(self, key: str) -> float:
        """Reserve the next slot for ``key``; return the seconds to wait for it.

        A caller that abandons its slot (a cancelled task, an exception between
        claiming and sending) leaves the reservation standing, costing a gap of
        throughput rather than exceeding the limit.
        """
        with self._lock:
            now = self._clock()
            slot = max(now, self._next.get(key, now))
            self._next[key] = slot + self._gap
            return slot - now


class AsyncRateLimiter:
    """Per-domain limit for coroutines; awaits its slot."""

    def __init__(self, rps: float = 1.0) -> None:
        self._scheduler = SlotScheduler(rps)

    async def wait(self, url: str) -> None:
        delay = self._scheduler.claim(domain_of(url))
        if delay > 0:
            await asyncio.sleep(delay)


class SyncRateLimiter:
    """Per-domain limit for blocking callers; sleeps for its slot.

    The sleep is outside the scheduler's lock, so threads waiting on different
    domains neither block each other nor queue up behind one another's sleeps.
    """

    def __init__(self, rps: float = 1.0) -> None:
        self._scheduler = SlotScheduler(rps)

    def wait(self, url: str) -> None:
        delay = self._scheduler.claim(domain_of(url))
        if delay > 0:
            time.sleep(delay)
