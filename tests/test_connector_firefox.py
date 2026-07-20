
import pytest

from pka.connectors.firefox import FirefoxBookmark, load_bookmarks


class TestLoadBookmarks:
    def test_returns_list_of_firefox_bookmarks(self, firefox_places_db, tmp_path):
        items = load_bookmarks(places_db=firefox_places_db)
        assert all(isinstance(i, FirefoxBookmark) for i in items)

    def test_correct_count(self, firefox_places_db):
        items = load_bookmarks(places_db=firefox_places_db)
        # Fixture has 2 unique bookmark URLs
        assert len(items) == 2

    def test_url_extracted(self, firefox_places_db):
        items = load_bookmarks(places_db=firefox_places_db)
        urls = {i.url for i in items}
        assert "https://raft.github.io" in urls

    def test_title_falls_back_to_page_title(self, firefox_places_db):
        # Bookmark 7 has no bm_title; should fall back to moz_places.title or url
        items = load_bookmarks(places_db=firefox_places_db)
        paxos = next(i for i in items if "paxos" in i.url)
        assert paxos.title  # not empty

    def test_folder_path_reconstructed(self, firefox_places_db):
        items = load_bookmarks(places_db=firefox_places_db)
        raft = next(i for i in items if "raft" in i.url)
        assert "Research" in raft.folder_path

    def test_tags_extracted(self, firefox_places_db):
        items = load_bookmarks(places_db=firefox_places_db)
        raft = next(i for i in items if "raft" in i.url)
        assert "consensus" in raft.tags

    def test_untagged_bookmark_has_empty_tags(self, firefox_places_db):
        items = load_bookmarks(places_db=firefox_places_db)
        paxos = next(i for i in items if "paxos" in i.url)
        assert paxos.tags == []

    def test_date_added_converted_from_microseconds(self, firefox_places_db):
        items = load_bookmarks(places_db=firefox_places_db)
        raft = next(i for i in items if "raft" in i.url)
        # 1680000000000 µs → 1680000000 s
        assert raft.date_added == 1680000000

    def test_deduplicates_urls(self, tmp_path):
        """Inserting the same URL in two folders should yield one bookmark."""
        import sqlite3
        db = tmp_path / "dup.sqlite"
        con = sqlite3.connect(db)
        con.executescript("""
            CREATE TABLE moz_places   (id INTEGER PRIMARY KEY, url TEXT, title TEXT);
            CREATE TABLE moz_bookmarks(id INTEGER PRIMARY KEY, type INTEGER,
                parent INTEGER, fk INTEGER, title TEXT, dateAdded INTEGER, guid TEXT);
            INSERT INTO moz_bookmarks VALUES (1,2,0,NULL,'root',0,'root________');
            INSERT INTO moz_bookmarks VALUES (2,2,1,NULL,'tags',0,'tags________');
            INSERT INTO moz_places    VALUES (1,'https://same.com','Same');
            INSERT INTO moz_bookmarks VALUES (3,1,1,1,'Same',1000,NULL);
            INSERT INTO moz_bookmarks VALUES (4,1,1,1,'Same again',2000,NULL);
        """)
        con.commit()
        con.close()
        items = load_bookmarks(places_db=db)
        assert len(items) == 1

    def test_raises_if_db_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_bookmarks(places_db=tmp_path / "no_such.sqlite")

    def test_auto_detects_default_release_profile(self, firefox_places_db, tmp_path):
        # Pass the firefox root dir (parent of profile dir) — should auto-detect
        firefox_root = firefox_places_db.parent.parent
        items = load_bookmarks(firefox_root=firefox_root)
        assert len(items) == 2

    def test_dev_mode_reuses_one_time_copy(self, firefox_places_db, monkeypatch):
        from pka import config

        monkeypatch.setattr(config.settings, "dev", True)
        firefox_root = firefox_places_db.parent.parent
        copy_path = config.settings.firefox_places_copy
        assert not copy_path.exists()

        first = load_bookmarks(firefox_root=firefox_root)
        assert copy_path.exists()
        assert len(first) == 2
        mtime = copy_path.stat().st_mtime

        second = load_bookmarks(firefox_root=firefox_root)
        assert copy_path.stat().st_mtime == mtime
        assert len(second) == 2

        load_bookmarks(firefox_root=firefox_root, refresh=True)
        assert copy_path.stat().st_mtime >= mtime
