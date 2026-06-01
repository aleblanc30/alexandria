"""Finite-support Gaussian-like kernel for bookmark-date trend reconstruction."""
from __future__ import annotations

import calendar
import datetime
import math
from collections import defaultdict
from collections.abc import Iterable

# Total support width: one calendar quarter approximated as 90 days (±45 days).
_SUPPORT_HALF_WIDTH_DAYS = 45.0
# σ = support/6 → kernel ≈ exp(-4.5) at the support boundary.
_SIGMA_DAYS = _SUPPORT_HALF_WIDTH_DAYS / 3.0


def finite_gaussian_kernel(delta_days: float) -> float:
    """Gaussian-like kernel with finite support centered at zero."""
    if abs(delta_days) > _SUPPORT_HALF_WIDTH_DAYS:
        return 0.0
    z = delta_days / _SIGMA_DAYS
    return math.exp(-0.5 * z * z)


def _month_center_utc(year: int, month: int) -> datetime.datetime:
    day = min(15, calendar.monthrange(year, month)[1])
    return datetime.datetime(year, month, day, 12, 0, 0, tzinfo=datetime.UTC)


def _month_key(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m")


def iter_month_centers(min_ts: float, max_ts: float) -> list[tuple[str, float]]:
    """Return ``[(YYYY-MM, center_unix_ts), ...]`` spanning the given range."""
    start = datetime.datetime.fromtimestamp(min_ts, datetime.UTC)
    end = datetime.datetime.fromtimestamp(max_ts, datetime.UTC)
    year, month = start.year, start.month
    end_key = (end.year, end.month)
    out: list[tuple[str, float]] = []
    while (year, month) <= end_key:
        center = _month_center_utc(year, month)
        out.append((_month_key(center), center.timestamp()))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return out


def build_kernel_timeline(
    rows: Iterable[tuple[str | None, float | int | None]],
) -> tuple[dict[str, dict[str, float]], dict[str, int]]:
    """Reconstruct per-label timelines from bookmark timestamps.

    Each document contributes a finite-support Gaussian-like kernel centered on
    its bookmark date. Values are evaluated at monthly grid points (month centers).
    """
    by_label: dict[str, list[float]] = defaultdict(list)
    for label, ts in rows:
        if ts is None:
            continue
        by_label[label or "Unlabelled"].append(float(ts))

    if not by_label:
        return {}, {}

    all_ts = [ts for stamps in by_label.values() for ts in stamps]
    half_support_sec = _SUPPORT_HALF_WIDTH_DAYS * 86400.0
    grid_start = min(all_ts) - half_support_sec
    grid_end = max(all_ts) + half_support_sec
    months = iter_month_centers(grid_start, grid_end)

    timeline: dict[str, dict[str, float]] = {}
    sizes: dict[str, int] = {}
    for label, timestamps in by_label.items():
        sizes[label] = len(timestamps)
        period_values: dict[str, float] = {}
        for month_key, center_ts in months:
            weight = sum(
                finite_gaussian_kernel((center_ts - ts) / 86400.0) for ts in timestamps
            )
            if weight:
                period_values[month_key] = weight
        timeline[label] = period_values

    return timeline, sizes
