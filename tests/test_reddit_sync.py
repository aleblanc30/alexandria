"""Reddit sync — metadata persistence and inline-body embedding (phase 1)."""
from __future__ import annotations

import sqlalchemy as sa

import pka.ingestion.reddit_sync as rs
from pka.connectors.reddit import load_saved as connector_load_saved
from pka.constants import FetchStatus, Source
from pka.db.queries import (
    document_has_chunks,
    document_index,
    get_engine,
    source_ingest_queue,
)
from pka.db.schema import documents, source_collections


def _patch_load(monkeypatch, fake_reddit_client):
    items = connector_load_saved(client=fake_reddit_client)
    monkeypatch.setattr(rs, "load_saved", lambda *a, **k: list(items))
    return items


def _fetch_status(source_id: str) -> str:
    with get_engine().connect() as con:
        return con.execute(
            sa.select(documents.c.fetch_status).where(
                (documents.c.source == str(Source.REDDIT))
                & (documents.c.source_id == source_id)
            )
        ).scalar_one()


def test_metadata_persists_all_items(monkeypatch, fake_reddit_client, mock_chroma):
    _patch_load(monkeypatch, fake_reddit_client)

    stats = rs.sync_reddit_metadata()

    assert stats["metadata"]["processed"] == 3
    index = document_index(Source.REDDIT)
    assert set(index) == {"t3_selfpost", "t3_linkpost", "t1_comment1"}

    # Self-post & comment carry inline content → AVAILABLE; link post → PENDING.
    assert _fetch_status("t3_selfpost") == FetchStatus.AVAILABLE
    assert _fetch_status("t1_comment1") == FetchStatus.AVAILABLE
    assert _fetch_status("t3_linkpost") == FetchStatus.PENDING


def test_subreddit_stored_as_collection(monkeypatch, fake_reddit_client, mock_chroma):
    _patch_load(monkeypatch, fake_reddit_client)
    rs.sync_reddit_metadata()

    doc_id = document_index(Source.REDDIT)["t3_selfpost"]
    with get_engine().connect() as con:
        cols = con.execute(
            sa.select(source_collections.c.collection).where(
                source_collections.c.document_id == doc_id
            )
        ).scalars().all()
    assert cols == ["r/compsci"]


def test_full_sync_embeds_inline_bodies_only(monkeypatch, fake_reddit_client, mock_chroma):
    _patch_load(monkeypatch, fake_reddit_client)

    stats = rs.sync_reddit()

    # Embed phase processes the self-post and comment; the link post is deferred.
    assert stats["embed"]["processed"] == 2
    index = document_index(Source.REDDIT)
    assert document_has_chunks(index["t3_selfpost"])
    assert document_has_chunks(index["t1_comment1"])
    assert not document_has_chunks(index["t3_linkpost"])


def test_ingest_queue_returns_only_pending_link_posts(
    monkeypatch, fake_reddit_client, mock_chroma,
):
    _patch_load(monkeypatch, fake_reddit_client)
    rs.sync_reddit_metadata()

    queue = source_ingest_queue(Source.REDDIT, None)
    doc_id = document_index(Source.REDDIT)["t3_linkpost"]
    # Only the PENDING link post is queued for fetch; AVAILABLE items are not.
    assert queue == [(doc_id, "https://example.com/paxos.pdf")]


def test_ingest_fetches_link_posts(monkeypatch, fake_reddit_client, mock_chroma):
    _patch_load(monkeypatch, fake_reddit_client)
    rs.sync_reddit_metadata()

    async def _fake_fetch(*, source, limit, progress_key, embed_fn, dry_run):
        work = source_ingest_queue(source, limit)
        embed = {"processed": 0, "skipped": 0, "failed": 0, "chunks": 0}
        for doc_id, _url in work:
            with get_engine().begin() as con:
                con.execute(
                    documents.update()
                    .where(documents.c.id == doc_id)
                    .values(fetch_status=str(FetchStatus.FETCHED))
                )
            long_text = (
                "Paxos is a family of protocols for solving consensus in a "
                "network of unreliable processors. Consensus is the process of "
                "agreeing on one result among a group of participants."
            )
            out = embed_fn(doc_id, long_text, None)
            if out["processed"]:
                embed["processed"] += 1
                embed["chunks"] += out["chunks"]
        return {"fetched": len(work), "skipped": 0, "unfetchable": 0, "embed": embed}

    monkeypatch.setattr(rs, "fetch_and_embed_pending", _fake_fetch)

    stats = rs.sync_reddit_ingest()

    assert stats["fetch"]["fetched"] == 1
    link_id = document_index(Source.REDDIT)["t3_linkpost"]
    assert document_has_chunks(link_id)


def test_reingest_is_idempotent(monkeypatch, fake_reddit_client, mock_chroma):
    _patch_load(monkeypatch, fake_reddit_client)

    rs.sync_reddit()
    stats = rs.sync_reddit()

    # Nothing new to import or embed on the second pass.
    assert stats["metadata"]["processed"] == 0
    assert stats["embed"]["processed"] == 0
