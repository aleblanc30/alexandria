"""Tests for the YouTube saved-videos connector, runner, and sync flow.

No network or OAuth is exercised: the connector is driven by the injected
``youtube_service`` fake and credential checks are stubbed where relevant.
"""

from __future__ import annotations

from pka.connectors.youtube import (
    YouTubeVideo,
    load_saved_videos,
    parse_timestamp,
    video_watch_url,
    youtube_card_summary,
    youtube_embed_text,
)
from pka.constants import Source, TagOrigin

# ── Pure helpers ──────────────────────────────────────────────────────────────


class TestHelpers:
    def test_video_watch_url(self):
        assert video_watch_url("abc123") == "https://www.youtube.com/watch?v=abc123"

    def test_parse_timestamp_rfc3339(self):
        # 2024-01-02T10:00:00Z
        assert parse_timestamp("2024-01-02T10:00:00Z") == 1704189600

    def test_parse_timestamp_none_and_garbage(self):
        assert parse_timestamp(None) is None
        assert parse_timestamp("not-a-date") is None

    def test_embed_text_includes_all_fields(self):
        v = YouTubeVideo(
            source_id="x",
            url="u",
            title="Title",
            channel="Chan",
            description="Desc",
            tags=["a", "b"],
        )
        text = youtube_embed_text(v)
        assert "Title" in text
        assert "Channel: Chan" in text
        assert "Desc" in text
        assert "Tags: a, b" in text

    def test_embed_text_skips_empty_fields(self):
        v = YouTubeVideo(
            source_id="x", url="u", title="Only Title", channel="", description="", tags=[]
        )
        assert youtube_embed_text(v) == "Only Title"

    def test_card_summary(self):
        v = YouTubeVideo(
            source_id="x", url="u", title="t", channel="c", description="  hello  ", tags=[]
        )
        assert youtube_card_summary(v) == "hello"
        v2 = YouTubeVideo(source_id="x", url="u", title="t", channel="c", description="", tags=[])
        assert youtube_card_summary(v2) is None


# ── Connector loading (fake service) ──────────────────────────────────────────


class TestLoadSavedVideos:
    def test_returns_youtube_videos(self, youtube_service):
        videos = load_saved_videos(service=youtube_service)
        assert all(isinstance(v, YouTubeVideo) for v in videos)

    def test_deduplicates_across_playlists(self, youtube_service):
        videos = load_saved_videos(service=youtube_service)
        ids = [v.source_id for v in videos]
        assert ids.count("vid_raft") == 1
        assert len(videos) == 2

    def test_multi_playlist_membership_recorded(self, youtube_service):
        videos = load_saved_videos(service=youtube_service)
        raft = next(v for v in videos if v.source_id == "vid_raft")
        assert "Liked videos" in raft.playlists
        assert "Conference Talks" in raft.playlists

    def test_earliest_added_date_wins(self, youtube_service):
        videos = load_saved_videos(service=youtube_service)
        raft = next(v for v in videos if v.source_id == "vid_raft")
        # Added to Conference Talks (2023-12-01) before Likes (2024-01-02)
        assert raft.date_added == parse_timestamp("2023-12-01T09:00:00Z")

    def test_hydrated_metadata(self, youtube_service):
        videos = load_saved_videos(service=youtube_service)
        raft = next(v for v in videos if v.source_id == "vid_raft")
        assert raft.title == "The Raft Consensus Algorithm"
        assert raft.channel == "Distributed Systems Talks"
        assert "consensus" in raft.tags
        assert raft.url == "https://www.youtube.com/watch?v=vid_raft"

    def test_missing_video_details_falls_back_to_id(self):
        from tests.conftest import FakeYouTubeService

        service = FakeYouTubeService(
            channels={"items": []},
            playlists={"items": [{"id": "PL1", "snippet": {"title": "P"}}]},
            playlist_items={
                "PL1": {
                    "items": [
                        {
                            "snippet": {
                                "publishedAt": "2024-01-01T00:00:00Z",
                                "resourceId": {"videoId": "ghost"},
                            },
                            "contentDetails": {"videoId": "ghost"},
                        },
                    ]
                }
            },
            videos={},  # no hydration data (private/deleted video)
        )
        videos = load_saved_videos(service=service)
        assert len(videos) == 1
        assert videos[0].title == "ghost"  # falls back to the id


# ── End-to-end sync (metadata + embed) ────────────────────────────────────────


def _patch_loader(monkeypatch, youtube_service):
    """Route the sync's connector call through the fake service."""
    from pka.ingestion import source_access

    def _fake_try_load():
        return load_saved_videos(service=youtube_service), None

    monkeypatch.setattr(source_access, "try_load_youtube_videos", _fake_try_load)
    # youtube_sync imported the symbol directly — patch it there too.
    import pka.ingestion.youtube_sync as ys

    monkeypatch.setattr(ys, "try_load_youtube_videos", _fake_try_load)


class TestSyncYoutube:
    def test_full_sync_persists_documents_and_chunks(
        self, youtube_service, monkeypatch, mock_chroma
    ):
        _patch_loader(monkeypatch, youtube_service)
        from pka.db.queries import document_index, source_ids_with_chunks
        from pka.ingestion.youtube_sync import sync_youtube

        result = sync_youtube()
        assert result["metadata"]["processed"] == 2
        assert result["embed"]["processed"] == 2

        docs = document_index(Source.YOUTUBE)
        assert set(docs) == {"vid_raft", "vid_paxos"}
        assert source_ids_with_chunks(Source.YOUTUBE) == {"vid_raft", "vid_paxos"}

    def test_metadata_writes_tags_collections_and_video_tag(self, youtube_service, monkeypatch):
        _patch_loader(monkeypatch, youtube_service)
        import sqlalchemy as sa

        from pka.db.queries import get_engine
        from pka.db.schema import overlay_tags, source_collections, source_tags
        from pka.ingestion.youtube_sync import sync_youtube_metadata

        sync_youtube_metadata()

        with get_engine().connect() as con:
            tags = {r[0] for r in con.execute(sa.select(source_tags.c.tag_string))}
            collections = {r[0] for r in con.execute(sa.select(source_collections.c.collection))}
            inferred = {
                r[0]
                for r in con.execute(
                    sa.select(overlay_tags.c.tag).where(
                        overlay_tags.c.origin == str(TagOrigin.INFERRED)
                    )
                )
            }
        assert "consensus" in tags
        assert {"Liked videos", "Conference Talks"} <= collections
        assert "video" in inferred

    def test_metadata_is_idempotent(self, youtube_service, monkeypatch):
        _patch_loader(monkeypatch, youtube_service)
        from pka.ingestion.youtube_sync import sync_youtube_metadata

        first = sync_youtube_metadata()
        second = sync_youtube_metadata()
        assert first["metadata"]["processed"] == 2
        assert second["metadata"]["skipped"] == 2
        assert second["metadata"]["processed"] == 0

    def test_unavailable_when_no_credentials(self, monkeypatch):
        import pka.ingestion.youtube_sync as ys
        from pka.ingestion import source_access

        def _unavailable():
            return [], "YouTube not configured."

        monkeypatch.setattr(ys, "try_load_youtube_videos", _unavailable)
        monkeypatch.setattr(source_access, "try_load_youtube_videos", _unavailable)

        result = ys.sync_youtube_metadata()
        assert result["unavailable"] == "YouTube not configured."
        assert result["metadata"]["processed"] == 0
