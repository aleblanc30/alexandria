"""Tests for cooperative sync stop checks."""
from pka.ingestion import progress as sp
from pka.ingestion.progress import should_stop


def test_none_key_returns_none():
    assert should_stop(None) is None
    assert should_stop("") is None


def test_no_job_returns_none():
    assert should_stop("zotero") is None


def test_reflects_cancel_request():
    sp.begin_job("zotero", "metadata")
    assert should_stop("zotero") is None
    sp.request_cancel("zotero")
    assert should_stop("zotero") == "cancel"
    sp.finish("zotero", stopped="cancel")
    assert should_stop("zotero") is None  # job no longer running


def test_reflects_pause_request():
    sp.begin_job("calibre", "ingest")
    sp.request_pause("calibre")
    assert should_stop("calibre") == "pause"
    sp.finish("calibre", stopped="pause")
