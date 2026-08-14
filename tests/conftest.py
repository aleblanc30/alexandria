"""
Shared fixtures for the Alexandria test suite.

Every test runs in an isolated ``tmp_path``; no real browser, Zotero, or
Calibre databases are touched. Ollama chat/vision and outbound HTTP calls are
never made — Chroma is replaced by ``mock_chroma``.
"""
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ── Settings override ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Redirect all data paths to a per-test temp directory and reset caches."""
    from pka import config
    s = config.settings
    monkeypatch.setattr(s, "data_dir",     tmp_path / "data")
    monkeypatch.setattr(s, "zotero_db",    tmp_path / "zotero.sqlite")
    monkeypatch.setattr(s, "firefox_db",   tmp_path / "firefox")
    monkeypatch.setattr(s, "book_archive", tmp_path / "books")
    monkeypatch.setattr(s, "image_dirs",   [tmp_path / "images"])

    # Pin provider selectors to their code defaults so a developer's local
    # ``.env`` (e.g. ALEXANDRIA_VISION_PROVIDER=openrouter) can't leak into the
    # suite and swap the backend a test mocks. Individual tests still override
    # these via monkeypatch where they exercise a specific provider.
    monkeypatch.setattr(s, "chat_provider",        "ollama")
    monkeypatch.setattr(s, "vision_provider",      "ollama")
    monkeypatch.setattr(s, "ocr_provider",         "vlm")
    monkeypatch.setattr(s, "image_embed_provider", "clip")

    # Reset cached SQLAlchemy engine so each test gets a fresh DB
    import pka.db.queries as q
    monkeypatch.setattr(q, "_engine", None)

    # Reset cached Chroma client/collection
    import pka.storage.vector_store as vs
    monkeypatch.setattr(vs, "_client", None)
    monkeypatch.setattr(vs, "_collection", None)

    # Reset cached CLIP collection (patch #9)
    import pka.ingestion.image_pipeline as ip
    monkeypatch.setattr(ip, "_clip_client", None)
    monkeypatch.setattr(ip, "_clip_col", None)

    # Reset cached provider instances so per-test config changes take effect
    import pka.providers as providers
    providers.reset_providers()

    # Reset in-memory sync progress so job state never leaks between tests
    from pka.constants import ALL_SOURCES
    from pka.ingestion import sync_progress as sp
    for src in ALL_SOURCES:
        sp.reset(src)

    yield

    for src in ALL_SOURCES:
        sp.reset(src)


# ── Fake Zotero SQLite ────────────────────────────────────────────────────────

def _make_zotero_db(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE itemTypes   (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
        CREATE TABLE items       (itemID INTEGER PRIMARY KEY, key TEXT,
                                  itemTypeID INTEGER, dateAdded TEXT);
        CREATE TABLE fields      (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        CREATE TABLE itemData    (itemID INTEGER, fieldID INTEGER, value TEXT);
        CREATE TABLE creators    (creatorID INTEGER PRIMARY KEY,
                                  firstName TEXT, lastName TEXT);
        CREATE TABLE creatorTypes(creatorTypeID INTEGER PRIMARY KEY,
                                  creatorType TEXT);
        CREATE TABLE itemCreators(itemID INTEGER, creatorID INTEGER,
                                  creatorTypeID INTEGER, orderIndex INTEGER);
        CREATE TABLE collections (collectionID INTEGER PRIMARY KEY,
                                  collectionName TEXT);
        CREATE TABLE collectionItems(collectionID INTEGER, itemID INTEGER);
        CREATE TABLE tags        (tagID INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE itemTags    (itemID INTEGER, tagID INTEGER);
        CREATE TABLE itemAttachments(itemID INTEGER, parentItemID INTEGER,
                                     contentType TEXT, path TEXT);
        CREATE TABLE itemAnnotations(itemID INTEGER, parentItemID INTEGER,
                                     type INTEGER, authorName TEXT, text TEXT,
                                     comment TEXT, color TEXT, pageLabel TEXT,
                                     sortIndex INTEGER, position TEXT,
                                     isExternal INTEGER);

        INSERT INTO itemTypes VALUES (1,'journalArticle'),(2,'attachment'),(3,'note'),(4,'annotation');
        INSERT INTO fields    VALUES (1,'title'),(2,'abstractNote'),(3,'DOI'),(4,'date');
        INSERT INTO creatorTypes VALUES (1,'author');

        -- Item 1: a journal article
        INSERT INTO items  VALUES (1,'RAFT0001',1,'2023-04-01T10:00:00');
        INSERT INTO itemData VALUES (1,1,'Raft Consensus'),
                                    (1,2,'A paper about Raft.'),
                                    (1,3,'10.1/raft'),
                                    (1,4,'2023');
        INSERT INTO creators VALUES (1,'Diego','Ongaro');
        INSERT INTO itemCreators VALUES (1,1,1,0);
        INSERT INTO collections VALUES (1,'Distributed Systems');
        INSERT INTO collectionItems VALUES (1,1);
        INSERT INTO tags VALUES (1,'consensus');
        INSERT INTO itemTags VALUES (1,1);
        -- PDF attachment for item 1
        INSERT INTO items VALUES (2,'RAFT0002',2,'2023-04-01T10:00:00');
        INSERT INTO itemAttachments VALUES (2,1,'application/pdf','storage:raft.pdf');

        -- Item 3: article without abstract or authors
        INSERT INTO items VALUES (3,'BARE0001',1,'2022-01-15T08:00:00');
        INSERT INTO itemData VALUES (3,1,'Bare Article');

        -- Item 4: PDF highlight annotation (Zotero 7)
        INSERT INTO items VALUES (4,'ANN00001',4,'2024-06-01T12:00:00');
        INSERT INTO itemAnnotations VALUES (4,1,1,NULL,
            'A highlighted passage long enough to embed from a PDF annotation.',
            NULL,NULL,NULL,0,NULL,0);
    """)
    con.commit()
    con.close()
    return path


@pytest.fixture()
def zotero_db(tmp_path) -> Path:
    return _make_zotero_db(tmp_path / "zotero.sqlite")


# ── Fake Firefox places.sqlite ────────────────────────────────────────────────

def _make_firefox_db(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE moz_places (
            id INTEGER PRIMARY KEY, url TEXT, title TEXT
        );
        CREATE TABLE moz_bookmarks (
            id INTEGER PRIMARY KEY, type INTEGER, parent INTEGER,
            fk INTEGER, title TEXT, dateAdded INTEGER, guid TEXT
        );

        INSERT INTO moz_bookmarks VALUES (1,2,0,NULL,'root',0,'root________');
        INSERT INTO moz_bookmarks VALUES (2,2,1,NULL,'Bookmarks Menu',0,'menu________');
        INSERT INTO moz_bookmarks VALUES (3,2,1,NULL,'Research',0,NULL);
        INSERT INTO moz_bookmarks VALUES (4,2,1,NULL,'tags',0,'tags________');
        INSERT INTO moz_bookmarks VALUES (5,2,4,NULL,'consensus',0,NULL);

        INSERT INTO moz_places VALUES (1,'https://raft.github.io','Raft Visualisation');
        INSERT INTO moz_places VALUES (2,'https://example.com/paxos','Paxos Made Simple');

        INSERT INTO moz_bookmarks VALUES (6,1,3,1,'Raft Visualisation',1680000000000000,NULL);
        INSERT INTO moz_bookmarks VALUES (7,1,2,2,NULL,1670000000000000,NULL);

        INSERT INTO moz_bookmarks VALUES (8,1,5,1,NULL,1680000000000000,NULL);
    """)
    con.commit()
    con.close()
    return path


@pytest.fixture()
def firefox_places_db(tmp_path) -> Path:
    profile_dir = tmp_path / "firefox" / "abc123.default-release"
    profile_dir.mkdir(parents=True)
    return _make_firefox_db(profile_dir / "places.sqlite")


# ── Fake YouTube Data API service ─────────────────────────────────────────────

class _FakeRequest:
    """Stands in for a googleapiclient request object (only ``execute`` used)."""

    def __init__(self, data: dict):
        self._data = data

    def execute(self) -> dict:
        return self._data


class _FakeEndpoint:
    def __init__(self, handler):
        self._handler = handler

    def list(self, **kwargs) -> _FakeRequest:
        return _FakeRequest(self._handler(kwargs))


class FakeYouTubeService:
    """Minimal duck-typed stand-in for the YouTube Data API v3 client.

    Single-page responses only (no ``nextPageToken``), which is all the
    connector's pagination loops need to terminate.
    """

    def __init__(self, *, channels=None, playlists=None, playlist_items=None, videos=None):
        self._channels = channels or {"items": []}
        self._playlists = playlists or {"items": []}
        self._playlist_items = playlist_items or {}   # playlist_id -> response
        self._videos = videos or {}                   # video_id -> snippet dict

    def channels(self):
        return _FakeEndpoint(lambda kw: self._channels)

    def playlists(self):
        return _FakeEndpoint(lambda kw: self._playlists)

    def playlistItems(self):  # noqa: N802 (mirror the API surface)
        return _FakeEndpoint(
            lambda kw: self._playlist_items.get(kw.get("playlistId"), {"items": []})
        )

    def videos(self):
        def _handle(kw):
            ids = [vid for vid in (kw.get("id") or "").split(",") if vid]
            return {
                "items": [
                    {"id": vid, "snippet": self._videos[vid]}
                    for vid in ids
                    if vid in self._videos
                ]
            }

        return _FakeEndpoint(_handle)


@pytest.fixture()
def youtube_service() -> FakeYouTubeService:
    """A fake service with two playlists sharing one video (dedupe coverage)."""
    return FakeYouTubeService(
        channels={
            "items": [
                {"contentDetails": {"relatedPlaylists": {"likes": "LL_LIKED"}}}
            ]
        },
        playlists={
            "items": [
                {"id": "PL_TALKS", "snippet": {"title": "Conference Talks"}},
            ]
        },
        playlist_items={
            "LL_LIKED": {
                "items": [
                    {
                        "snippet": {
                            "publishedAt": "2024-01-02T10:00:00Z",
                            "resourceId": {"videoId": "vid_raft"},
                        },
                        "contentDetails": {"videoId": "vid_raft"},
                    },
                    {
                        "snippet": {
                            "publishedAt": "2024-03-01T08:00:00Z",
                            "resourceId": {"videoId": "vid_paxos"},
                        },
                        "contentDetails": {"videoId": "vid_paxos"},
                    },
                ]
            },
            "PL_TALKS": {
                "items": [
                    {
                        # Same video, added to this playlist earlier than to Likes
                        "snippet": {
                            "publishedAt": "2023-12-01T09:00:00Z",
                            "resourceId": {"videoId": "vid_raft"},
                        },
                        "contentDetails": {"videoId": "vid_raft"},
                    },
                ]
            },
        },
        videos={
            "vid_raft": {
                "title": "The Raft Consensus Algorithm",
                "channelTitle": "Distributed Systems Talks",
                "description": "A talk explaining the Raft consensus protocol.",
                "tags": ["raft", "consensus", "distributed systems"],
            },
            "vid_paxos": {
                "title": "Paxos Made Live",
                "channelTitle": "Systems Channel",
                "description": "Lessons from implementing Paxos in production.",
                "tags": ["paxos"],
            },
        },
    )


# ── Fake Reddit saved listing (PRAW-compatible client) ────────────────────────

def _make_reddit_saved_items():
    """Sample saved items: a self-post, a link post, and a comment.

    Attribute shapes mirror praw ``Submission`` / ``Comment`` objects closely
    enough for the connector, without importing praw.
    """
    from types import SimpleNamespace

    self_post = SimpleNamespace(
        name="t3_selfpost",
        title="Ask HN: favourite consensus algorithm?",
        selftext="I keep coming back to Raft for its understandability.",
        is_self=True,
        url="https://www.reddit.com/r/compsci/comments/selfpost/",
        permalink="/r/compsci/comments/selfpost/ask/",
        subreddit="compsci",
        created_utc=1700000000,
    )
    link_post = SimpleNamespace(
        name="t3_linkpost",
        title="Paxos Made Simple (PDF)",
        selftext="",
        is_self=False,
        url="https://example.com/paxos.pdf",
        permalink="/r/distributed/comments/linkpost/paxos/",
        subreddit="distributed",
        created_utc=1700000100,
    )
    comment = SimpleNamespace(
        name="t1_comment1",
        body="Raft's leader election is the clearest part of the protocol.",
        link_title="Understanding Raft",
        permalink="/r/compsci/comments/xyz/understanding_raft/c1/",
        subreddit="compsci",
        created_utc=1700000200,
    )
    return [self_post, link_post, comment]


@pytest.fixture()
def fake_reddit_client():
    """A MagicMock PRAW client whose saved() yields the sample items."""
    items = _make_reddit_saved_items()
    client = MagicMock()
    client.user.me.return_value.saved.return_value = iter(items)
    # Re-create the iterator on each saved() call so multiple loads work.
    client.user.me.return_value.saved.side_effect = (
        lambda *a, **k: iter(_make_reddit_saved_items())
    )
    return client


FAKE_DIM = 8   # tiny dimension for mock Chroma vectors


def fake_embedding(text: str) -> list[float]:
    """Deterministic fake embedding: ASCII sum spread over FAKE_DIM dims."""
    total = sum(ord(c) for c in text)
    return [(total % (i + 2)) / 100.0 for i in range(FAKE_DIM)]


# ── Mock Chroma ───────────────────────────────────────────────────────────────

@pytest.fixture()
def empty_vector_store(monkeypatch):
    """Mocked Chroma collection returning no results — default for API tests."""
    col = MagicMock()
    col.count.return_value = 0
    col.query.return_value = {
        "ids": [[]], "documents": [[]], "distances": [[]], "metadatas": [[]],
    }
    col.get.return_value = {
        "ids": [], "embeddings": [], "metadatas": [], "documents": [],
    }
    import pka.storage.vector_store as vs
    monkeypatch.setattr(vs, "_collection", col)
    monkeypatch.setattr(vs, "get_collection", lambda: col)
    return col


@pytest.fixture()
def mock_chroma(monkeypatch):
    """Replace Chroma with an in-memory dict store."""
    store: dict[str, dict] = {}

    col = MagicMock()

    def _upsert(ids, documents, metadatas, embeddings=None, **kwargs):
        for i, vid in enumerate(ids):
            emb = (
                embeddings[i]
                if embeddings is not None
                else fake_embedding(documents[i])
            )
            store[vid] = {
                "text": documents[i],
                "meta": metadatas[i],
                "emb":  emb,
            }

    def _query(query_texts=None, query_embeddings=None, n_results=10, **kw):
        """Rank stored items by L2 distance to the query embedding.

        Real distances (instead of a constant) so ordering and similarity
        logic in search code is actually exercised.
        """
        if query_embeddings is not None:
            q_emb = list(query_embeddings[0])
        elif query_texts is not None:
            q_emb = fake_embedding(query_texts[0])
        else:
            q_emb = [0.0] * FAKE_DIM

        def _dist(emb: list[float]) -> float:
            return sum((a - b) ** 2 for a, b in zip(q_emb, emb, strict=False)) ** 0.5

        ranked = sorted(
            ((vid, item, _dist(item["emb"])) for vid, item in store.items()),
            key=lambda t: t[2],
        )[:n_results]
        return {
            "ids":       [[vid for vid, _, _ in ranked]],
            "documents": [[item["text"] for _, item, _ in ranked]],
            "distances": [[d for _, _, d in ranked]],
            "metadatas": [[item["meta"] for _, item, _ in ranked]],
        }

    def _get(ids=None, include=None, **kwargs):
        ids = ids or []
        metadatas = [store[vid]["meta"] for vid in ids if vid in store]
        embeddings = [store[vid]["emb"] for vid in ids if vid in store]
        out: dict = {"ids": [vid for vid in ids if vid in store]}
        if include:
            if "metadatas" in include:
                out["metadatas"] = metadatas
            if "embeddings" in include:
                out["embeddings"] = embeddings
        return out

    col.upsert.side_effect = _upsert
    col.query.side_effect  = _query
    col.get.side_effect    = _get
    col.count.return_value = 0

    import pka.storage.vector_store as vs
    monkeypatch.setattr(vs, "get_collection", lambda: col)
    return store, col
