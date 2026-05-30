"""
Shared fixtures for the PKA test suite.

Every test runs in an isolated ``tmp_path``; no real browser, Zotero, or
Calibre databases are touched. Ollama and outbound HTTP calls are never
made — they are patched at the module boundary by ``mock_embedder`` and
``mock_chroma``.
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
    monkeypatch.setattr(s, "images_dir",   tmp_path / "images")

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

    yield


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


# ── Mock embedder ─────────────────────────────────────────────────────────────

FAKE_DIM = 8   # tiny dimension for tests


def fake_embedding(text: str) -> list[float]:
    """Deterministic fake embedding: ASCII sum spread over FAKE_DIM dims."""
    total = sum(ord(c) for c in text)
    return [(total % (i + 2)) / 100.0 for i in range(FAKE_DIM)]


@pytest.fixture()
def mock_embedder(monkeypatch):
    import pka.ingestion.embedder as emb
    monkeypatch.setattr(emb, "embed_one",   fake_embedding)
    monkeypatch.setattr(
        emb, "embed_batch",
        lambda texts, **kw: [fake_embedding(t) for t in texts],
    )
    return fake_embedding


# ── Mock Chroma ───────────────────────────────────────────────────────────────

@pytest.fixture()
def mock_chroma(monkeypatch):
    """Replace Chroma with an in-memory dict store."""
    store: dict[str, dict] = {}

    col = MagicMock()

    def _upsert(ids, embeddings, documents, metadatas):
        for i, vid in enumerate(ids):
            store[vid] = {
                "text": documents[i],
                "meta": metadatas[i],
                "emb":  embeddings[i],
            }

    def _query(query_embeddings, n_results=10, **kw):
        items = list(store.values())[:n_results]
        return {
            "ids":       [[v for v in store.keys()][:n_results]],
            "documents": [[i["text"] for i in items]],
            "distances": [[0.1] * len(items)],
            "metadatas": [[i["meta"] for i in items]],
        }

    col.upsert.side_effect = _upsert
    col.query.side_effect  = _query
    col.count.return_value = 0

    import pka.storage.vector_store as vs
    monkeypatch.setattr(vs, "get_collection", lambda: col)
    return store, col
