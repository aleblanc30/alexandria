"""Reddit sync — metadata persistence and inline-body embedding (phase 1)."""

from __future__ import annotations

import sqlalchemy as sa

import pka.ingestion.reddit_sync as rs
from pka.constants import FetchStatus, Source
from pka.db.queries import (
    document_has_chunks,
    document_index,
    get_engine,
    source_ingest_queue,
)
from pka.db.schema import documents, source_collections


def _patch_load(monkeypatch, reddit_saved_items):
    monkeypatch.setattr(rs, "load_saved", lambda *a, **k: list(reddit_saved_items))
    return reddit_saved_items


def _fetch_status(source_id: str) -> str:
    with get_engine().connect() as con:
        return con.execute(
            sa.select(documents.c.fetch_status).where(
                (documents.c.source == str(Source.REDDIT)) & (documents.c.source_id == source_id)
            )
        ).scalar_one()


def test_metadata_persists_all_items(monkeypatch, reddit_saved_items, mock_chroma):
    _patch_load(monkeypatch, reddit_saved_items)

    stats = rs.sync_reddit_metadata()

    assert stats["metadata"]["processed"] == 3
    index = document_index(Source.REDDIT)
    assert set(index) == {"t3_selfpost", "t3_linkpost", "t1_comment1"}

    # Self-post & comment carry inline content → AVAILABLE; link post → PENDING.
    assert _fetch_status("t3_selfpost") == FetchStatus.AVAILABLE
    assert _fetch_status("t1_comment1") == FetchStatus.AVAILABLE
    assert _fetch_status("t3_linkpost") == FetchStatus.PENDING


def test_subreddit_stored_as_collection(monkeypatch, reddit_saved_items, mock_chroma):
    _patch_load(monkeypatch, reddit_saved_items)
    rs.sync_reddit_metadata()

    doc_id = document_index(Source.REDDIT)["t3_selfpost"]
    with get_engine().connect() as con:
        cols = (
            con.execute(
                sa.select(source_collections.c.collection).where(
                    source_collections.c.document_id == doc_id
                )
            )
            .scalars()
            .all()
        )
    assert cols == ["r/compsci"]


def test_full_sync_embeds_inline_bodies_only(monkeypatch, reddit_saved_items, mock_chroma):
    _patch_load(monkeypatch, reddit_saved_items)

    stats = rs.sync_reddit()

    # Embed phase processes the self-post and comment; the link post is deferred.
    assert stats["embed"]["processed"] == 2
    index = document_index(Source.REDDIT)
    assert document_has_chunks(index["t3_selfpost"])
    assert document_has_chunks(index["t1_comment1"])
    assert not document_has_chunks(index["t3_linkpost"])


def test_ingest_does_not_repoll_the_live_feed(monkeypatch, reddit_saved_items, mock_chroma):
    """The ingest phase reads bodies back from SQLite instead of a second feed walk.

    Metadata already persists kind/subreddit/permalink/external_url/body for
    every item it sees, so a second live poll right after would only cost an
    extra request against Reddit's API for no new information.
    """
    _patch_load(monkeypatch, reddit_saved_items)
    rs.sync_reddit_metadata()

    def _no_polling(*a, **k):
        raise AssertionError("ingest must not poll the live feed a second time")

    monkeypatch.setattr(rs, "load_saved", _no_polling)

    stats = rs.sync_reddit_ingest()

    assert stats["embed"]["processed"] == 2


def test_ingest_queue_returns_only_pending_link_posts(
    monkeypatch,
    reddit_saved_items,
    mock_chroma,
):
    _patch_load(monkeypatch, reddit_saved_items)
    rs.sync_reddit_metadata()

    queue = source_ingest_queue(Source.REDDIT, None)
    doc_id = document_index(Source.REDDIT)["t3_linkpost"]
    # Only the PENDING link post is queued for fetch; AVAILABLE items are not.
    assert queue == [(doc_id, "https://example.com/paxos.pdf")]


def test_ingest_fetches_link_posts(monkeypatch, reddit_saved_items, mock_chroma):
    _patch_load(monkeypatch, reddit_saved_items)
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


def test_link_post_embeds_title(monkeypatch, reddit_saved_items, mock_chroma):
    """Fetched link-post text carries the post title into the index (DESIGN §3.2)."""
    from pka.ingestion.runners.reddit import embed_fetched_text

    store, _ = mock_chroma
    _patch_load(monkeypatch, reddit_saved_items)
    rs.sync_reddit_metadata()
    doc_id = document_index(Source.REDDIT)["t3_linkpost"]

    body = (
        "The protocol tolerates crash failures. A quorum of acceptors must "
        "promise before a value can be chosen by any proposer."
    )
    embed_fetched_text(doc_id, body, skip_existing=False)

    records = [it for it in store.values() if it["meta"]["document_id"] == doc_id]
    assert records
    assert all(r["meta"]["title"] == "Paxos Made Simple (PDF)" for r in records)
    assert any("Paxos Made Simple (PDF)" in r["text"] for r in records)


def test_thin_link_post_still_gets_one_chunk(monkeypatch, reddit_saved_items, mock_chroma):
    from pka.ingestion.runners.reddit import embed_fetched_text

    _patch_load(monkeypatch, reddit_saved_items)
    rs.sync_reddit_metadata()
    doc_id = document_index(Source.REDDIT)["t3_linkpost"]

    outcome = embed_fetched_text(doc_id, "404.", skip_existing=False)

    assert outcome["processed"] and outcome["chunks"] == 1
    assert document_has_chunks(doc_id)


def test_reingest_is_idempotent(monkeypatch, reddit_saved_items, mock_chroma):
    _patch_load(monkeypatch, reddit_saved_items)

    rs.sync_reddit()
    stats = rs.sync_reddit()

    # Nothing new to import or embed on the second pass.
    assert stats["metadata"]["processed"] == 0
    assert stats["embed"]["processed"] == 0


# ── reddit_items detail row ───────────────────────────────────────────────────


def _reddit_row(source_id: str):
    from pka.db.queries import reddit_item

    doc_id = document_index(Source.REDDIT)[source_id]
    with get_engine().connect() as con:
        return reddit_item(con, doc_id)


def test_metadata_persists_reddit_detail_fields(
    monkeypatch,
    reddit_saved_items,
    mock_chroma,
):
    _patch_load(monkeypatch, reddit_saved_items)
    rs.sync_reddit_metadata()

    comment = _reddit_row("t1_comment1")
    assert comment["kind"] == "comment"
    assert comment["subreddit"] == "compsci"
    assert comment["external_url"] is None
    # The body verbatim, not the 280-char card excerpt.
    assert comment["body"] == ("Raft's leader election is the clearest part of the protocol.")


def test_link_post_keeps_permalink_separate_from_external_url(
    monkeypatch,
    reddit_saved_items,
    mock_chroma,
):
    """The saved thread survives even though url_or_path is the external target."""
    _patch_load(monkeypatch, reddit_saved_items)
    rs.sync_reddit_metadata()

    row = _reddit_row("t3_linkpost")
    assert row["external_url"] == "https://example.com/paxos.pdf"
    assert row["permalink"] == ("https://www.reddit.com/r/distributed/comments/linkpost/paxos/")

    doc_id = document_index(Source.REDDIT)["t3_linkpost"]
    with get_engine().connect() as con:
        url = con.execute(
            sa.select(documents.c.url_or_path).where(documents.c.id == doc_id)
        ).scalar_one()
    assert url == "https://example.com/paxos.pdf"


def test_metadata_rerun_backfills_detail_rows(
    monkeypatch,
    reddit_saved_items,
    mock_chroma,
):
    """A library archived before ``reddit_items`` existed fills in on the next run.

    Simulated by deleting the rows after a first sync: the documents stay, and a
    second metadata pass — which reports every item as skipped — must restore
    them without needing a dedicated backfill.
    """
    from pka.db.schema import reddit_items

    _patch_load(monkeypatch, reddit_saved_items)
    rs.sync_reddit_metadata()
    with get_engine().begin() as con:
        con.execute(sa.delete(reddit_items))
    assert _reddit_row("t1_comment1") is None

    stats = rs.sync_reddit_metadata()

    assert stats["metadata"]["processed"] == 0
    assert stats["metadata"]["skipped"] == 3
    assert _reddit_row("t1_comment1")["kind"] == "comment"


def test_dry_run_metadata_does_not_write_detail_rows(
    monkeypatch,
    reddit_saved_items,
    mock_chroma,
):
    _patch_load(monkeypatch, reddit_saved_items)

    stats = rs.sync_reddit_metadata(dry_run=True)

    # Nothing archived yet, so every item still counts as new work.
    assert stats["metadata"]["processed"] == 3
    assert document_index(Source.REDDIT) == {}


# ── Generated summaries for inline bodies ─────────────────────────────────────


def _long_body(word: str = "Consensus") -> str:
    return " ".join(f"{word} detail number {i} matters here." for i in range(40))


def _summary_chunks(mock_chroma) -> list[dict]:
    store, _col = mock_chroma
    return [i["meta"] for i in store.values() if i["meta"].get("pass") == "summary"]


def test_inline_bodies_get_no_summary_when_the_flag_is_off(
    monkeypatch,
    reddit_saved_items,
    mock_chroma,
):
    _patch_load(monkeypatch, reddit_saved_items)
    rs.sync_reddit_metadata()
    rs.sync_reddit_ingest()

    assert _summary_chunks(mock_chroma) == []


def test_long_inline_body_gets_a_summary_chunk(
    monkeypatch,
    reddit_saved_items,
    mock_chroma,
):
    """Self-posts and comments owe a summary just as fetched link posts do."""
    from pka.config import settings as cfg
    from pka.ingestion import summarize as sz

    monkeypatch.setattr(cfg, "bookmark_summary_enabled", True)
    monkeypatch.setattr(
        sz,
        "chat_json",
        lambda *a, **k: ({"summary": "Consensus protocols and their trade-offs."}, None),
    )

    items = _patch_load(monkeypatch, reddit_saved_items)
    for item in items:
        if item.body:
            item.body = _long_body()
    rs.sync_reddit_metadata()
    rs.sync_reddit_ingest()

    # The self-post and the comment; the link post is the fetcher's job.
    assert len(_summary_chunks(mock_chroma)) == 2


def test_comment_summary_is_framed_as_a_comment_with_its_thread(
    monkeypatch,
    reddit_saved_items,
    mock_chroma,
):
    """The summariser is told what the text is and which thread it came from."""
    from pka.config import settings as cfg
    from pka.ingestion import summarize as sz

    monkeypatch.setattr(cfg, "bookmark_summary_enabled", True)
    prompts: list[str] = []

    def _record(prompt, **kwargs):
        prompts.append(prompt)
        return ({"summary": "Leader election in Raft."}, None)

    monkeypatch.setattr(sz, "chat_json", _record)

    items = _patch_load(monkeypatch, reddit_saved_items)
    for item in items:
        if item.kind == "comment":
            item.body = _long_body("Raft")
        else:
            item.body = None
    rs.sync_reddit_metadata()
    rs.sync_reddit_ingest()

    assert len(prompts) == 1
    assert "indexing a Reddit comment" in prompts[0]
    assert "Understanding Raft" in prompts[0]
    assert "Subreddit: r/compsci" in prompts[0]


def test_metadata_can_be_replayed_from_the_archive(monkeypatch, reddit_saved_items, mock_chroma):
    """--from-archive rebuilds the rows from disk without polling the feed."""
    from dataclasses import asdict

    from pka.connectors import reddit_archive

    reddit_archive.record_items([asdict(item) for item in reddit_saved_items])

    def _no_polling(*a, **k):
        raise AssertionError("the feed must not be polled with from_archive=True")

    monkeypatch.setattr(rs, "load_saved", _no_polling)

    stats = rs.sync_reddit_metadata(from_archive=True)

    assert stats["metadata"]["processed"] == 3
    assert set(document_index(Source.REDDIT)) == {
        "t3_selfpost",
        "t3_linkpost",
        "t1_comment1",
    }


def test_metadata_replays_the_archive_backlog_before_polling(
    monkeypatch, reddit_saved_items, mock_chroma
):
    """Items the archive holds but the database does not are ingested off disk."""
    from dataclasses import asdict

    from pka.connectors import reddit_archive

    reddit_archive.record_items([asdict(item) for item in reddit_saved_items])

    stop_sets: list[set[str]] = []

    def _empty_feed(*a, **k):
        stop_sets.append(set(k.get("known_ids") or ()))
        return []

    monkeypatch.setattr(rs, "load_saved", _empty_feed)

    stats = rs.sync_reddit_metadata()

    assert stats["metadata"]["processed"] == 3
    assert set(document_index(Source.REDDIT)) == {
        "t3_selfpost",
        "t3_linkpost",
        "t1_comment1",
    }
    # The backlog ids stop the walk too, so the feed is not asked for them.
    assert stop_sets == [{"t3_selfpost", "t3_linkpost", "t1_comment1"}]


def test_backfill_prefers_the_polled_copy_over_the_archived_one(
    monkeypatch, reddit_saved_items, mock_chroma
):
    """A backfill walk ignores the stop signal, so the two lists can overlap."""
    from dataclasses import asdict, replace

    from pka.connectors import reddit_archive

    reddit_archive.record_items([asdict(item) for item in reddit_saved_items])
    fresh = [replace(item, title=f"{item.title} (edited)") for item in reddit_saved_items]
    monkeypatch.setattr(rs, "load_saved", lambda *a, **k: list(fresh))

    stats = rs.sync_reddit_metadata(backfill=True)

    assert stats["metadata"]["processed"] == 3
    with get_engine().connect() as con:
        titles = set(
            con.execute(
                sa.select(documents.c.title).where(documents.c.source == str(Source.REDDIT))
            )
            .scalars()
            .all()
        )
    assert all(t.endswith("(edited)") for t in titles)
