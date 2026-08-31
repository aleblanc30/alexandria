"""Tests for source-vs-archive pending metadata counts."""

import time

import pytest
from PIL import Image as PILImage

import pka.ingestion.pending_metadata as pm
from pka.config import settings
from pka.connectors.images import ImageFile
from pka.constants import Source
from pka.db.queries import DocumentWrite, init_db, insert_document_if_new, record_image_rejection
from pka.ingestion.pending_metadata import (
    archive_document_count,
    count_pending_metadata,
    invalidate_source_probes,
    source_corpus_size,
)
from pka.ingestion.runners.firefox import ingest_firefox_bookmarks
from tests.test_pipeline import _make_firefox_bookmark


def test_archive_document_count():
    init_db()
    assert archive_document_count(Source.FIREFOX) == 0
    insert_document_if_new(DocumentWrite("firefox", "bm1", "T", "http://a", None))
    assert archive_document_count(Source.FIREFOX) == 1


def test_count_pending_skips_archived_firefox(monkeypatch):
    init_db()
    bm = _make_firefox_bookmark()
    ingest_firefox_bookmarks([bm])
    monkeypatch.setattr(
        "pka.connectors.firefox.load_bookmarks",
        lambda: [bm, _make_firefox_bookmark(source_id="bm-new", url="http://new")],
    )
    assert count_pending_metadata(Source.FIREFOX) == 1


def test_probe_result_is_cached_within_ttl(monkeypatch):
    """count/corpus probes must not re-hit the source on every status poll."""
    init_db()
    calls = {"n": 0}

    def _one_bookmark():
        calls["n"] += 1
        return [_make_firefox_bookmark(source_id="bm-1", url="http://a")]

    monkeypatch.setattr("pka.connectors.firefox.load_bookmarks", _one_bookmark)

    assert source_corpus_size(Source.FIREFOX) == 1
    assert source_corpus_size(Source.FIREFOX) == 1  # served from cache
    assert calls["n"] == 1

    invalidate_source_probes(Source.FIREFOX)
    assert source_corpus_size(Source.FIREFOX) == 1  # recomputed after invalidation
    assert calls["n"] == 2


def test_probe_cache_disabled_when_ttl_zero(monkeypatch):
    init_db()
    calls = {"n": 0}

    def _one_bookmark():
        calls["n"] += 1
        return [_make_firefox_bookmark(source_id="bm-1", url="http://a")]

    monkeypatch.setattr("pka.connectors.firefox.load_bookmarks", _one_bookmark)
    monkeypatch.setattr(pm.settings, "ingestion_probe_cache_ttl_seconds", 0.0)

    source_corpus_size(Source.FIREFOX)
    source_corpus_size(Source.FIREFOX)
    assert calls["n"] == 2  # no caching, recomputed each call


# ── Image counters vs the admission-gate rejection cache ──────────────────────
# Both image passes skip paths in the rejection cache, so the probes that drive
# the progress bars must scope their counts the same way. Counting a rejected
# image as outstanding work pins a phase total the job can never reach, which is
# what made a metadata sync sit at "8 / 10" forever.


def _image_file(path) -> ImageFile:
    PILImage.new("RGB", (40, 30), color="white").save(path)
    return ImageFile(path, path.name, 40, 30, 1000, int(time.time()), {})


@pytest.fixture()
def three_scanned_images(tmp_path, monkeypatch) -> list[ImageFile]:
    init_db()
    imgs = [_image_file(tmp_path / f"img{i}.png") for i in range(3)]
    monkeypatch.setattr(pm, "try_scan_images", lambda: (imgs, None))
    return imgs


def test_image_pending_excludes_gate_rejections(three_scanned_images, monkeypatch):
    monkeypatch.setattr(settings, "image_gate_enabled", True)
    assert count_pending_metadata(Source.IMAGE) == 3

    record_image_rejection(str(three_scanned_images[0].path), "not_category_of_interest")
    invalidate_source_probes(Source.IMAGE)
    assert count_pending_metadata(Source.IMAGE) == 2


def test_image_corpus_size_excludes_gate_rejections(three_scanned_images, monkeypatch):
    monkeypatch.setattr(settings, "image_gate_enabled", True)
    record_image_rejection(str(three_scanned_images[0].path), "low_text_coverage")
    assert source_corpus_size(Source.IMAGE) == 2


def test_image_counters_ignore_rejections_when_gate_disabled(
    three_scanned_images,
    monkeypatch,
):
    """With the gate off both passes re-admit every path, so nothing is excluded."""
    monkeypatch.setattr(settings, "image_gate_enabled", False)
    record_image_rejection(str(three_scanned_images[0].path), "low_text_coverage")
    assert count_pending_metadata(Source.IMAGE) == 3
    assert source_corpus_size(Source.IMAGE) == 3


def test_metadata_job_total_is_reachable_with_rejections(
    three_scanned_images,
    monkeypatch,
):
    """The total a metadata job pins must equal what the pass can actually persist."""
    from pka.ingestion.image_pipeline import register_images

    monkeypatch.setattr(settings, "image_gate_enabled", True)
    record_image_rejection(str(three_scanned_images[0].path), "not_category_of_interest")
    invalidate_source_probes(Source.IMAGE)

    baseline = archive_document_count(Source.IMAGE)
    pinned_total = baseline + count_pending_metadata(Source.IMAGE)

    register_images(three_scanned_images)

    assert archive_document_count(Source.IMAGE) == pinned_total
