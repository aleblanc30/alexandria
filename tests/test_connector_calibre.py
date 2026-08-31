"""
Calibre connector tests.
Uses a synthetic metadata.db built in tmp_path — no real Calibre installation needed.
"""

import sqlite3
from pathlib import Path

import pytest

from pka.connectors.calibre import CalibreBook, load_books, split_calibre_tags


def _book_by_title(items: list[CalibreBook], title: str) -> CalibreBook:
    return next(b for b in items if b.title == title)


# ── Fixture: minimal Calibre library ─────────────────────────────────────────


def _make_calibre_library(root: Path) -> Path:
    """
    Build a minimal but schema-accurate Calibre library in `root`.
    Returns the root path.
    """
    root.mkdir(parents=True, exist_ok=True)
    db = root / "metadata.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE books (
            id           INTEGER PRIMARY KEY,
            title        TEXT NOT NULL DEFAULT 'Unknown',
            sort         TEXT,
            timestamp    TEXT,
            pubdate      TEXT,
            series_index REAL DEFAULT 1.0,
            author_sort  TEXT,
            isbn         TEXT DEFAULT '',
            lccn         TEXT DEFAULT '',
            path         TEXT NOT NULL DEFAULT '',
            flags        INTEGER DEFAULT 1,
            uuid         TEXT,
            has_cover    BOOL DEFAULT 0,
            last_modified TEXT NOT NULL DEFAULT '2000-01-01 00:00:00+00:00'
        );
        CREATE TABLE ratings (
            id     INTEGER PRIMARY KEY,
            rating INTEGER CHECK(rating > -1 AND rating < 11)
        );
        CREATE TABLE books_ratings_link (
            id     INTEGER PRIMARY KEY,
            book   INTEGER NOT NULL,
            rating INTEGER NOT NULL
        );
        CREATE TABLE authors (
            id   INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            sort TEXT
        );
        CREATE TABLE books_authors_link (
            id     INTEGER PRIMARY KEY,
            book   INTEGER NOT NULL,
            author INTEGER NOT NULL
        );
        CREATE TABLE tags (
            id   INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );
        CREATE TABLE books_tags_link (
            id   INTEGER PRIMARY KEY,
            book INTEGER NOT NULL,
            tag  INTEGER NOT NULL
        );
        CREATE TABLE publishers (
            id   INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            sort TEXT
        );
        CREATE TABLE books_publishers_link (
            id        INTEGER PRIMARY KEY,
            book      INTEGER NOT NULL,
            publisher INTEGER NOT NULL
        );
        CREATE TABLE series (
            id   INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            sort TEXT
        );
        CREATE TABLE books_series_link (
            id     INTEGER PRIMARY KEY,
            book   INTEGER NOT NULL,
            series INTEGER NOT NULL
        );
        CREATE TABLE comments (
            id   INTEGER PRIMARY KEY,
            book INTEGER NOT NULL,
            text TEXT NOT NULL
        );
        CREATE TABLE identifiers (
            id   INTEGER PRIMARY KEY,
            book INTEGER NOT NULL,
            type TEXT NOT NULL,
            val  TEXT NOT NULL
        );
        CREATE TABLE data (
            id     INTEGER PRIMARY KEY,
            book   INTEGER NOT NULL,
            format TEXT NOT NULL,
            uncompressed_size INTEGER,
            name   TEXT NOT NULL
        );

        -- Book 1: fully populated
        INSERT INTO books VALUES (
            1,'Thinking, Fast and Slow',NULL,
            '2023-05-01 10:00:00+00:00','2011-01-01 00:00:00+00:00',
            1.0,'Kahneman, Daniel','978-0374533557','',
            'Daniel Kahneman/Thinking, Fast and Slow (1)',
            1,'uuid-1',1,'2023-05-01 10:00:00+00:00'
        );
        INSERT INTO authors VALUES (1,'Daniel Kahneman','Kahneman, Daniel');
        INSERT INTO books_authors_link VALUES (1,1,1);
        INSERT INTO tags VALUES (1,'psychology'),(2,'economics'),(3,'non-fiction');
        INSERT INTO books_tags_link VALUES (1,1,1),(2,1,2),(3,1,3);
        INSERT INTO publishers VALUES (1,'Farrar, Straus and Giroux',NULL);
        INSERT INTO books_publishers_link VALUES (1,1,1);
        INSERT INTO comments VALUES (1,1,'A landmark book on decision-making and cognitive biases.');
        INSERT INTO identifiers VALUES (1,1,'isbn','978-0374533557');
        INSERT INTO data VALUES (1,1,'EPUB',1024000,'Thinking, Fast and Slow - Daniel Kahneman');
        INSERT INTO data VALUES (2,1,'PDF', 2048000,'Thinking, Fast and Slow - Daniel Kahneman');
        INSERT INTO ratings VALUES (1,8);
        INSERT INTO books_ratings_link VALUES (1,1,1);

        -- Book 2: part of a series, no description
        INSERT INTO books VALUES (
            2,'The Fellowship of the Ring',NULL,
            '2022-11-10 08:00:00+00:00','1954-01-01 00:00:00+00:00',
            1.0,'Tolkien, J.R.R.',NULL,'',
            'J.R.R. Tolkien/The Fellowship of the Ring (2)',
            1,'uuid-2',0,'2022-11-10 08:00:00+00:00'
        );
        INSERT INTO authors VALUES (2,'J.R.R. Tolkien','Tolkien, J.R.R.');
        INSERT INTO books_authors_link VALUES (2,2,2);
        INSERT INTO tags VALUES (4,'fantasy'),(5,'fiction');
        INSERT INTO books_tags_link VALUES (4,2,4),(5,2,5);
        INSERT INTO series VALUES (1,'The Lord of the Rings',NULL);
        INSERT INTO books_series_link VALUES (1,2,1);
        INSERT INTO data VALUES (3,2,'EPUB',3000000,'The Fellowship of the Ring - J.R.R. Tolkien');
        INSERT INTO ratings VALUES (2,10);
        INSERT INTO books_ratings_link VALUES (2,2,2);

        -- Book 3: no formats registered
        INSERT INTO books VALUES (
            3,'Bare Book',NULL,'2021-01-01 00:00:00+00:00',NULL,
            1.0,NULL,NULL,'','Bare Book (3)',
            1,'uuid-3',0,'2021-01-01 00:00:00+00:00'
        );
        INSERT INTO authors VALUES (3,'Anonymous','Anonymous');
        INSERT INTO books_authors_link VALUES (3,3,3);
    """)
    con.commit()
    con.close()

    # Create stub EPUB files so preferred_path resolution finds them
    for sub, fname in [
        (
            "Daniel Kahneman/Thinking, Fast and Slow (1)",
            "Thinking, Fast and Slow - Daniel Kahneman.epub",
        ),
        (
            "J.R.R. Tolkien/The Fellowship of the Ring (2)",
            "The Fellowship of the Ring - J.R.R. Tolkien.epub",
        ),
    ]:
        f = root / sub / fname
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"PK")  # minimal stub (not a real EPUB)

    return root


@pytest.fixture()
def calibre_library(tmp_path) -> Path:
    return _make_calibre_library(tmp_path / "CalibreLibrary")


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestLoadBooks:
    def test_returns_list_of_calibre_books(self, calibre_library, tmp_path):
        items = load_books(library_root=calibre_library, copy_path=tmp_path / "copy.db")
        assert all(isinstance(b, CalibreBook) for b in items)

    def test_correct_book_count(self, calibre_library, tmp_path):
        items = load_books(library_root=calibre_library, copy_path=tmp_path / "copy.db")
        assert len(items) == 3

    def test_title_extracted(self, calibre_library, tmp_path):
        items = load_books(library_root=calibre_library, copy_path=tmp_path / "copy.db")
        titles = {b.title for b in items}
        assert "Thinking, Fast and Slow" in titles

    def test_authors_extracted(self, calibre_library, tmp_path):
        items = load_books(library_root=calibre_library, copy_path=tmp_path / "copy.db")
        book = _book_by_title(items, "Thinking, Fast and Slow")
        assert "Daniel Kahneman" in book.authors

    def test_description_extracted(self, calibre_library, tmp_path):
        items = load_books(library_root=calibre_library, copy_path=tmp_path / "copy.db")
        book = _book_by_title(items, "Thinking, Fast and Slow")
        assert book.description is not None
        assert "decision-making" in book.description

    def test_tags_extracted_verbatim(self, calibre_library, tmp_path):
        items = load_books(library_root=calibre_library, copy_path=tmp_path / "copy.db")
        book = _book_by_title(items, "Thinking, Fast and Slow")
        assert "psychology" in book.tags
        assert "non-fiction" in book.tags

    def test_publisher_extracted(self, calibre_library, tmp_path):
        items = load_books(library_root=calibre_library, copy_path=tmp_path / "copy.db")
        book = _book_by_title(items, "Thinking, Fast and Slow")
        assert book.publisher == "Farrar, Straus and Giroux"

    def test_series_extracted(self, calibre_library, tmp_path):
        items = load_books(library_root=calibre_library, copy_path=tmp_path / "copy.db")
        book = next(b for b in items if "Fellowship" in b.title)
        assert book.series == "The Lord of the Rings"

    def test_no_series_is_none(self, calibre_library, tmp_path):
        items = load_books(library_root=calibre_library, copy_path=tmp_path / "copy.db")
        book = _book_by_title(items, "Thinking, Fast and Slow")
        assert book.series is None

    def test_isbn_extracted(self, calibre_library, tmp_path):
        items = load_books(library_root=calibre_library, copy_path=tmp_path / "copy.db")
        book = _book_by_title(items, "Thinking, Fast and Slow")
        assert book.isbn == "978-0374533557"

    def test_formats_list_populated(self, calibre_library, tmp_path):
        items = load_books(library_root=calibre_library, copy_path=tmp_path / "copy.db")
        book = _book_by_title(items, "Thinking, Fast and Slow")
        assert "EPUB" in book.formats
        assert "PDF" in book.formats

    def test_epub_preferred_over_pdf(self, calibre_library, tmp_path):
        items = load_books(library_root=calibre_library, copy_path=tmp_path / "copy.db")
        book = _book_by_title(items, "Thinking, Fast and Slow")
        assert book.preferred_path is not None
        assert book.preferred_path.suffix.lower() == ".epub"

    def test_preferred_path_exists_on_disk(self, calibre_library, tmp_path):
        items = load_books(library_root=calibre_library, copy_path=tmp_path / "copy.db")
        book = _book_by_title(items, "Thinking, Fast and Slow")
        assert book.preferred_path.exists()

    def test_no_formats_gives_none_path(self, calibre_library, tmp_path):
        items = load_books(library_root=calibre_library, copy_path=tmp_path / "copy.db")
        bare = next(b for b in items if b.title == "Bare Book")
        assert bare.preferred_path is None

    def test_year_parsed_from_pubdate(self, calibre_library, tmp_path):
        items = load_books(library_root=calibre_library, copy_path=tmp_path / "copy.db")
        book = _book_by_title(items, "Thinking, Fast and Slow")
        assert book.year == 2011

    def test_no_pubdate_gives_none_year(self, calibre_library, tmp_path):
        items = load_books(library_root=calibre_library, copy_path=tmp_path / "copy.db")
        bare = next(b for b in items if b.title == "Bare Book")
        assert bare.year is None

    def test_date_added_is_unix_timestamp(self, calibre_library, tmp_path):
        items = load_books(library_root=calibre_library, copy_path=tmp_path / "copy.db")
        book = _book_by_title(items, "Thinking, Fast and Slow")
        assert isinstance(book.date_added, int)
        assert book.date_added > 0

    def test_rating_extracted(self, calibre_library, tmp_path):
        items = load_books(library_root=calibre_library, copy_path=tmp_path / "copy.db")
        book = _book_by_title(items, "Thinking, Fast and Slow")
        assert book.rating == 8

    def test_no_description_is_none(self, calibre_library, tmp_path):
        items = load_books(library_root=calibre_library, copy_path=tmp_path / "copy.db")
        book = next(b for b in items if "Fellowship" in b.title)
        assert book.description is None

    def test_raises_if_metadata_db_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_books(library_root=tmp_path / "NonExistent", copy_path=tmp_path / "copy.db")

    def test_source_id_is_string(self, calibre_library, tmp_path):
        items = load_books(library_root=calibre_library, copy_path=tmp_path / "copy.db")
        for b in items:
            assert isinstance(b.source_id, str)


class TestSplitCalibreTags:
    def test_short_tags_kept_no_note(self):
        tags, note = split_calibre_tags(["psychology", "non-fiction", "history of rome"])
        assert tags == ["psychology", "non-fiction", "history of rome"]
        assert note is None

    def test_four_word_tag_is_kept(self):
        # "more than 4 words" is the note threshold — exactly 4 stays a tag
        tags, note = split_calibre_tags(["a b c d"])
        assert tags == ["a b c d"]
        assert note is None

    def test_five_word_tag_becomes_note(self):
        tags, note = split_calibre_tags(["imported from zotero on friday"])
        assert tags == []
        assert note == "imported from zotero on friday"

    def test_mixed_tags_partitioned(self):
        tags, note = split_calibre_tags(
            [
                "economics",
                "leftover note that is quite long",
                "psychology",
            ]
        )
        assert tags == ["economics", "psychology"]
        assert note == "leftover note that is quite long"

    def test_multiple_long_tags_bundled_newline_separated(self):
        tags, note = split_calibre_tags(
            [
                "first long leftover import note",
                "second long leftover import note",
            ]
        )
        assert tags == []
        assert note == ("first long leftover import note\nsecond long leftover import note")

    def test_empty_tags(self):
        assert split_calibre_tags([]) == ([], None)
