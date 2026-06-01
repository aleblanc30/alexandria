"""
Read-only Calibre library connector.
Reads metadata.db via a temporary copy (no lock contention with the running
Calibre GUI), then resolves the on-disk file path for each book's preferred
format (EPUB > PDF > first available).

File layout assumed (Calibre default):
  <library_root>/
    metadata.db
    <Author Name>/
      <Title> (<id>)/
        <Title> - <Author>.<ext>
        cover.jpg
        metadata.opf
"""
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pka.config import settings as cfg
from pka.db.sqlite_copy import copy_sqlite_database

log = logging.getLogger(__name__)

# Preference order for picking a format to extract text from
_FORMAT_PREFERENCE = ["EPUB", "PDF", "MOBI", "AZW3", "AZW", "TXT", "HTML"]


@dataclass
class CalibreBook:
    source_id: str              # str(calibre book id)
    title: str
    authors: list[str]
    description: str | None     # HTML; caller should strip tags if needed
    publisher: str | None
    series: str | None
    series_index: float | None
    year: int | None            # from pubdate
    isbn: str | None
    tags: list[str]             # Calibre tags (verbatim)
    formats: list[str]          # available format extensions, e.g. ['EPUB','PDF']
    preferred_path: Path | None # path to the preferred readable format
    date_added: int | None      # unix timestamp (timestamp field)
    rating: int | None          # 0–10 in Calibre DB (displayed as 0–5 stars)


# ── DB copy ───────────────────────────────────────────────────────────────────

def _copy_db(library_root: Path) -> Path:
    src = library_root / "metadata.db"
    if not src.exists():
        raise FileNotFoundError(f"Calibre metadata.db not found at {src}")
    dst = cfg.data_dir / "calibre_metadata_copy.db"
    return copy_sqlite_database(src, dst)


# ── Format path resolution ────────────────────────────────────────────────────

def _resolve_format_path(
    library_root: Path,
    book_path: str,       # relative path stored in books.path, e.g. "Author/Title (1)"
    formats: dict[str, str],  # {EXT: filename_without_ext}
) -> Path | None:
    """
    Return the absolute path to the preferred readable format, or None.
    Calibre stores the filename stem in data.name; the full filename is
    <name>.<ext.lower()> inside the book's subfolder.
    """
    for fmt in _FORMAT_PREFERENCE:
        name = formats.get(fmt)
        if name:
            candidate = library_root / book_path / f"{name}.{fmt.lower()}"
            if candidate.exists():
                return candidate
    # Fallback: return first existing format in any order
    for fmt, name in formats.items():
        candidate = library_root / book_path / f"{name}.{fmt.lower()}"
        if candidate.exists():
            return candidate
    return None


# ── Timestamp parsing ─────────────────────────────────────────────────────────

def _parse_ts(dt_str: str | None) -> int | None:
    """Convert Calibre ISO datetime string (UTC) to unix timestamp."""
    if not dt_str:
        return None
    try:
        from datetime import datetime, timezone
        # Calibre uses 'YYYY-MM-DD HH:MM:SS.ffffff+00:00' or similar
        dt_str = dt_str.split("+")[0].strip()  # strip tz suffix
        dt = datetime.fromisoformat(dt_str)
        return int(dt.replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return None


def _parse_year(pubdate: str | None) -> int | None:
    if not pubdate:
        return None
    try:
        year = int(pubdate[:4])
        return year if year > 1000 else None
    except Exception:
        return None


# ── Main loader ───────────────────────────────────────────────────────────────

def load_books(
    library_root: Path | None = None,
    copy_path: Path | None = None,
) -> list[CalibreBook]:
    """
    Load all books from a Calibre library.

    Args:
        library_root: Path to the Calibre library folder (contains metadata.db).
                      Defaults to settings.book_archive.
        copy_path:    Where to write the DB copy. Defaults to data_dir/calibre_copy.db.
    """
    root = library_root or cfg.book_archive
    src = root / "metadata.db"
    if not src.exists():
        raise FileNotFoundError(f"Calibre metadata.db not found at {src}")

    db_copy = copy_path or (cfg.data_dir / "calibre_metadata_copy.db")
    if copy_path:
        copy_sqlite_database(src, db_copy)
    else:
        db_copy = _copy_db(root)

    books: list[CalibreBook] = []

    with sqlite3.connect(db_copy) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()

        # ── Core book rows ────────────────────────────────────────────────────
        cur.execute("""
            SELECT
                b.id, b.title, b.path, b.pubdate, b.timestamp,
                b.series_index, b.rating,
                (SELECT text FROM comments WHERE book = b.id LIMIT 1) AS description
            FROM books b
            ORDER BY b.id
        """)
        rows = cur.fetchall()
        log.info("Found %d books in Calibre library", len(rows))

        for row in rows:
            book_id = row["id"]

            # Authors
            cur.execute("""
                SELECT a.name FROM authors a
                JOIN books_authors_link bal ON a.id = bal.author
                WHERE bal.book = ?
                ORDER BY bal.id
            """, (book_id,))
            authors = [r["name"] for r in cur.fetchall()]

            # Tags (verbatim)
            cur.execute("""
                SELECT t.name FROM tags t
                JOIN books_tags_link btl ON t.id = btl.tag
                WHERE btl.book = ?
            """, (book_id,))
            tags = [r["name"] for r in cur.fetchall()]

            # Publisher
            cur.execute("""
                SELECT p.name FROM publishers p
                JOIN books_publishers_link bpl ON p.id = bpl.publisher
                WHERE bpl.book = ?
                LIMIT 1
            """, (book_id,))
            pub_row = cur.fetchone()
            publisher = pub_row["name"] if pub_row else None

            # Series
            cur.execute("""
                SELECT s.name FROM series s
                JOIN books_series_link bsl ON s.id = bsl.series
                WHERE bsl.book = ?
                LIMIT 1
            """, (book_id,))
            ser_row = cur.fetchone()
            series = ser_row["name"] if ser_row else None

            # Identifiers (ISBN, DOI, …)
            cur.execute("""
                SELECT type, val FROM identifiers WHERE book = ?
            """, (book_id,))
            identifiers = {r["type"]: r["val"] for r in cur.fetchall()}
            isbn = identifiers.get("isbn")

            # Formats: {EXT: name_stem}
            cur.execute("""
                SELECT format, name FROM data WHERE book = ?
            """, (book_id,))
            formats_map: dict[str, str] = {
                r["format"].upper(): r["name"]
                for r in cur.fetchall()
            }

            preferred_path = _resolve_format_path(root, row["path"], formats_map)

            books.append(CalibreBook(
                source_id      = str(book_id),
                title          = row["title"] or "",
                authors        = authors,
                description    = row["description"],
                publisher      = publisher,
                series         = series,
                series_index   = row["series_index"],
                year           = _parse_year(row["pubdate"]),
                isbn           = isbn,
                tags           = tags,
                formats        = list(formats_map.keys()),
                preferred_path = preferred_path,
                date_added     = _parse_ts(row["timestamp"]),
                rating         = row["rating"],
            ))

    log.info("Loaded %d Calibre books", len(books))
    return books
