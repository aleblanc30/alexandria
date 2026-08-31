"""
Forward-migration tests for ``init_db``.

``meta.create_all()`` creates missing *tables* but never touches a table that
already exists, so every column and index added to ``pka/db/schema.py`` after a
table shipped needs a hand-written ``ALTER TABLE`` / ``CREATE INDEX`` in
``init_db``. Nothing enforces that pairing: omit the ALTER and ``init_db`` still
exits 0 against an existing archive, which then fails at query time with
``no such column``. These tests are that enforcement.

``_V1_SCHEMA_SQL`` is a frozen snapshot of the schema as first committed. It is
deliberately never updated — it is the oldest archive ``alexandria init`` claims
to migrate, so a column added to ``schema.py`` without a matching migration
shows up here as a diff against a freshly created database.
"""

import sqlite3

import pytest
import sqlalchemy as sa

from pka.config import settings
from pka.db.queries import get_engine, init_db
from pka.db.schema import meta

# Frozen: the schema at the first commit. Do not regenerate — see module docstring.
_V1_SCHEMA_SQL = """
CREATE TABLE documents (
    id INTEGER NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT,
    url_or_path TEXT,
    date_added INTEGER,
    ingested_at INTEGER,
    fetch_status TEXT,
    PRIMARY KEY (id),
    CONSTRAINT uq_source_item UNIQUE (source, source_id)
);
CREATE TABLE source_tags (
    id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    tag_string TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(document_id) REFERENCES documents (id)
);
CREATE TABLE source_collections (
    id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    collection TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(document_id) REFERENCES documents (id)
);
CREATE TABLE chunks (
    id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    token_count INTEGER,
    vector_id TEXT,
    PRIMARY KEY (id),
    FOREIGN KEY(document_id) REFERENCES documents (id)
);
CREATE TABLE fetch_log (
    id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    timestamp INTEGER NOT NULL,
    http_status INTEGER,
    error_msg TEXT,
    PRIMARY KEY (id),
    FOREIGN KEY(document_id) REFERENCES documents (id)
);
CREATE TABLE overlay_tags (
    id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    origin TEXT NOT NULL,
    confidence FLOAT,
    created_at INTEGER,
    PRIMARY KEY (id),
    FOREIGN KEY(document_id) REFERENCES documents (id)
);
CREATE TABLE cluster_runs (
    run_id INTEGER NOT NULL,
    timestamp INTEGER NOT NULL,
    algorithm TEXT,
    parameters TEXT,
    accepted BOOLEAN,
    status TEXT,
    notes TEXT,
    umap_points TEXT,
    PRIMARY KEY (run_id)
);
CREATE TABLE clusters (
    cluster_id INTEGER NOT NULL,
    label TEXT,
    description TEXT,
    created_at INTEGER,
    run_id INTEGER,
    PRIMARY KEY (cluster_id),
    FOREIGN KEY(run_id) REFERENCES cluster_runs (run_id)
);
CREATE TABLE cluster_assignments (
    id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    cluster_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    score FLOAT,
    assigned_at INTEGER,
    PRIMARY KEY (id),
    FOREIGN KEY(document_id) REFERENCES documents (id),
    FOREIGN KEY(cluster_id) REFERENCES clusters (cluster_id),
    FOREIGN KEY(run_id) REFERENCES cluster_runs (run_id)
);
CREATE TABLE reading_lists (
    list_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    created_at INTEGER,
    PRIMARY KEY (list_id)
);
CREATE TABLE reading_list_items (
    id INTEGER NOT NULL,
    list_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    position INTEGER,
    note TEXT,
    PRIMARY KEY (id),
    FOREIGN KEY(list_id) REFERENCES reading_lists (list_id),
    FOREIGN KEY(document_id) REFERENCES documents (id)
);
CREATE TABLE images (
    id INTEGER NOT NULL,
    path TEXT NOT NULL,
    filename TEXT NOT NULL,
    image_type TEXT,
    width INTEGER,
    height INTEGER,
    file_size INTEGER,
    date_taken INTEGER,
    ocr_text TEXT,
    description TEXT,
    clip_vector_id TEXT,
    text_vector_id TEXT,
    indexed_at INTEGER,
    PRIMARY KEY (id),
    UNIQUE (path)
);
CREATE TABLE image_tags (
    id INTEGER NOT NULL,
    image_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    origin TEXT NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(image_id) REFERENCES images (id)
);
"""

# Every column ``init_db`` claims to add to a pre-existing table. Dropping one
# and re-running init must put it back; a migration deleted or mis-guarded here
# leaves a populated archive stale.
_MIGRATED_COLUMNS = [
    ("documents", "ingested_at"),
    ("documents", "generated_summary"),
    ("documents", "zotero_attachment_key"),
    ("documents", "archive_url"),
    ("documents", "item_type"),
    ("documents", "card_summary"),
    ("documents", "note"),
    ("documents", "doc_embedding"),
    ("documents", "doi"),
    ("documents", "arxiv_id"),
    ("documents", "isbn"),
    ("documents", "year"),
    ("documents", "authors_json"),
    ("documents", "zotero_url"),
    ("documents", "zotero_path"),
    ("chunks", "chunk_pass"),
    ("chunks", "resolved_by"),
    ("chunks", "source_ref"),
    ("chunks", "ref_title"),
    ("images", "document_id"),
    ("images", "books_json"),
    ("cluster_runs", "umap_points"),
    ("cluster_runs", "status"),
    ("clusters", "level"),
    ("clusters", "parent_cluster_id"),
    ("cluster_assignments", "level"),
]

_MIGRATED_INDEXES = [
    ("chunks", "ix_chunks_document_id"),
    ("documents", "ix_documents_source"),
    ("documents", "ix_documents_doi"),
    ("documents", "ix_documents_arxiv_id"),
    ("documents", "ix_documents_isbn"),
    ("overlay_tags", "uq_overlay_doc_tag_origin"),
]


def _normalise_default(dflt):
    """SQLite echoes a server_default back quoted ('1') where an ALTER writes 1.

    The stored value is identical either way — an INTEGER column yields the
    integer 1 from both — so the quoting is not a difference worth failing on.
    """
    if isinstance(dflt, str):
        return dflt.strip("'\"")
    return dflt


def _snapshot(db_path) -> dict:
    """Tables → columns and indexes, in a form two archives can be compared by."""
    con = sqlite3.connect(db_path)
    try:
        out = {}
        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for t in tables:
            # Column order is not compared: ALTER appends, CREATE TABLE declares.
            cols = {
                r[1]: (r[2].upper(), r[3], _normalise_default(r[4]))
                for r in con.execute(f"PRAGMA table_info({t})")
            }
            named, unique_cols = {}, []
            for r in con.execute(f"PRAGMA index_list({t})"):
                members = tuple(x[2] for x in con.execute(f"PRAGMA index_info({r[1]})"))
                if r[1].startswith("sqlite_autoindex"):
                    unique_cols.append(members)  # UNIQUE constraints get generated names
                else:
                    named[r[1]] = (bool(r[2]), members)
            out[t] = {"columns": cols, "indexes": named, "unique": sorted(unique_cols)}
        return out
    finally:
        con.close()


def _head_schema(tmp_path) -> dict:
    """What ``create_all`` produces today — the target every archive must reach."""
    path = tmp_path / "head.db"
    eng = sa.create_engine(f"sqlite:///{path}")
    meta.create_all(eng)
    eng.dispose()  # Windows locks an open SQLite file; release before reading
    return _snapshot(path)


def _write_v1_archive() -> None:
    """Lay down a first-commit archive where ``init_db`` will find it."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(settings.archive_db)
    try:
        con.executescript(_V1_SCHEMA_SQL)
        con.commit()
    finally:
        con.close()


def _seed_v1_rows() -> None:
    """Populate the v1 archive: migrations must run against data, not an empty file."""
    con = sqlite3.connect(settings.archive_db)
    try:
        con.execute(
            "INSERT INTO documents (source, source_id, title, url_or_path, date_added,"
            " ingested_at, fetch_status) VALUES ('firefox', 'seed-1', 'Seeded doc',"
            " 'https://example.invalid/seed', 1700000000, 1700000000, 'fetched')"
        )
        con.execute(
            "INSERT INTO chunks (document_id, chunk_index, text, token_count)"
            " VALUES (1, 0, 'seed chunk text', 3)"
        )
        # Three identical overlay tags: legal in v1, and the unique index cannot be
        # created until init_db's dedupe removes two of them.
        for _ in range(3):
            con.execute(
                "INSERT INTO overlay_tags (document_id, tag, origin, confidence)"
                " VALUES (1, 'dupe-tag', 'llm', 0.9)"
            )
        con.execute(
            "INSERT INTO cluster_runs (timestamp, algorithm) VALUES (1700000000, 'hdbscan')"
        )
        con.execute("INSERT INTO clusters (label, run_id) VALUES ('seed cluster', 1)")
        con.execute(
            "INSERT INTO cluster_assignments (document_id, cluster_id, run_id) VALUES (1, 1, 1)"
        )
        con.execute("INSERT INTO images (path, filename) VALUES ('/seed/img.png', 'img.png')")
        con.commit()
    finally:
        con.close()


class TestForwardMigration:
    """A first-commit archive must reach today's schema through ``init_db`` alone."""

    def test_v1_archive_migrates_to_head_schema(self, tmp_path):
        head = _head_schema(tmp_path)
        _write_v1_archive()
        _seed_v1_rows()

        init_db()

        migrated = _snapshot(settings.archive_db)
        assert set(migrated) == set(head), (
            "tables differ from a freshly created archive: "
            f"missing={sorted(set(head) - set(migrated))} "
            f"unexpected={sorted(set(migrated) - set(head))}"
        )
        for table in sorted(head):
            missing = set(head[table]["columns"]) - set(migrated[table]["columns"])
            assert not missing, (
                f"init_db left {table} without {sorted(missing)}. A column was added to "
                "schema.py without an ALTER TABLE in init_db, so existing archives keep "
                "the old table and fail at query time with 'no such column'."
            )
            assert migrated[table]["columns"] == head[table]["columns"], (
                f"{table}: column definitions diverge from a fresh archive"
            )
            assert migrated[table]["indexes"] == head[table]["indexes"], (
                f"init_db left {table} without the indexes create_all declares. "
                "create_all skips indexes on tables that already exist, so a new "
                "sa.Index needs a CREATE INDEX IF NOT EXISTS in init_db."
            )
            assert migrated[table]["unique"] == head[table]["unique"], (
                f"{table}: UNIQUE constraints diverge from a fresh archive"
            )

    def test_v1_data_survives_migration(self):
        _write_v1_archive()
        _seed_v1_rows()

        init_db()

        with get_engine().connect() as con:
            assert (
                con.execute(
                    sa.text("SELECT title FROM documents WHERE source_id = 'seed-1'")
                ).scalar()
                == "Seeded doc"
            )
            assert con.execute(sa.text("SELECT COUNT(*) FROM chunks")).scalar() == 1
            # Dedupe keeps exactly one of the three identical tags.
            assert con.execute(sa.text("SELECT COUNT(*) FROM overlay_tags")).scalar() == 1
            assert con.execute(sa.text("PRAGMA integrity_check")).scalar() == "ok"
            assert con.execute(sa.text("PRAGMA foreign_key_check")).fetchall() == []

    def test_migrating_a_v1_archive_twice_is_a_no_op(self):
        _write_v1_archive()
        _seed_v1_rows()
        init_db()
        once = _snapshot(settings.archive_db)

        init_db()

        assert _snapshot(settings.archive_db) == once


class TestMigratedColumns:
    """Each ALTER in ``init_db`` must actually restore its column."""

    @pytest.mark.parametrize(("table", "column"), _MIGRATED_COLUMNS)
    def test_dropped_column_is_restored(self, table, column):
        init_db()
        con = sqlite3.connect(settings.archive_db)
        try:
            con.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            con.commit()
        except sqlite3.OperationalError as exc:
            # SQLite refuses to drop an indexed or FK-referenced column; those are
            # covered by the v1 forward-migration test instead.
            pytest.skip(f"{table}.{column} is not droppable in SQLite: {exc}")
        finally:
            con.close()

        init_db()

        with get_engine().connect() as con:
            cols = [r[1] for r in con.execute(sa.text(f"PRAGMA table_info({table})"))]
        assert column in cols, (
            f"init_db did not re-add {table}.{column}; its ALTER TABLE is missing or "
            "guarded by the wrong condition"
        )

    @pytest.mark.parametrize(("table", "index"), _MIGRATED_INDEXES)
    def test_dropped_index_is_restored(self, table, index):
        init_db()
        con = sqlite3.connect(settings.archive_db)
        try:
            con.execute(f"DROP INDEX {index}")
            con.commit()
        finally:
            con.close()

        init_db()

        with get_engine().connect() as con:
            names = [r[1] for r in con.execute(sa.text(f"PRAGMA index_list({table})"))]
        assert index in names, f"init_db did not recreate {index} on an existing archive"
