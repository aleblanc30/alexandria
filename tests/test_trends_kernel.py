"""Tests for trend kernel reconstruction."""
import datetime

from pka.trends.kernel import (
    build_kernel_timeline,
    finite_gaussian_kernel,
    iter_month_centers,
)


def test_finite_gaussian_kernel_support():
    assert finite_gaussian_kernel(0.0) == 1.0
    assert finite_gaussian_kernel(45.0) > 0.0
    assert finite_gaussian_kernel(45.01) == 0.0
    assert finite_gaussian_kernel(-45.01) == 0.0


def test_iter_month_centers_spans_range():
    start = datetime.datetime(2024, 1, 10).timestamp()
    end = datetime.datetime(2024, 3, 20).timestamp()
    months = iter_month_centers(start, end)
    keys = [k for k, _ in months]
    assert keys == ["2024-01", "2024-02", "2024-03"]


def test_build_kernel_timeline_single_bookmark_peak_near_date():
    center = datetime.datetime(2024, 6, 15, 12, 0, 0, tzinfo=datetime.UTC)
    ts = center.timestamp()
    timeline, sizes = build_kernel_timeline([("Topic", ts)])
    assert sizes == {"Topic": 1}
    assert timeline["Topic"]["2024-06"] == 1.0
    assert timeline["Topic"].get("2024-05", 0) < 1.0
    assert timeline["Topic"].get("2024-07", 0) < 1.0


def test_build_kernel_timeline_multiple_labels():
    ts = datetime.datetime(2024, 1, 15, 12, 0, 0, tzinfo=datetime.UTC).timestamp()
    timeline, sizes = build_kernel_timeline([("A", ts), ("B", ts), ("A", ts)])
    assert sizes == {"A": 2, "B": 1}
    assert timeline["A"]["2024-01"] == 2.0
    assert timeline["B"]["2024-01"] == 1.0
