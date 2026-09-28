"""Create ``archive.db``'s tables and bring an existing archive up to date.

``meta.create_all()`` creates missing *tables* but never alters one that
already exists, so every column or index added to :mod:`pka.db.schema` after its
table shipped also needs a step in :data:`MIGRATIONS`.

Each step is a ``(name, fn)`` pair. ``init_db`` runs the steps in list order,
each in its own transaction together with a ``schema_migrations`` row recording
its name, and skips any step already recorded. Every step is still idempotent
(it checks before it alters), because an archive that predates the record runs
all of them once, including the ones whose changes it already has.

Adding a step: append it, with a name that is new. Never rename, reorder or
edit a step that has shipped — an archive that recorded it will not run the
new version.
"""

import time
from collections.abc import Callable

import sqlalchemy as sa

from pka.db import engine
from pka.db.schema import meta, schema_migrations

Step = Callable[[sa.Connection], None]


def _columns(con: sa.Connection, table: str) -> list[str]:
    """Column names of ``table``, or ``[]`` when the table does not exist."""
    return [r[1] for r in con.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()]


def _add_column(table: str, column: str, ddl: str) -> Step:
    """Step that adds ``column`` to ``table`` as ``ddl`` if it is missing."""

    def step(con: sa.Connection) -> None:
        cols = _columns(con, table)
        if cols and column not in cols:
            con.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))

    return step


def _create_index(name: str, table: str, columns: str, *, unique: bool = False) -> Step:
    """Step that creates an index if it does not exist.

    ``create_all()`` skips indexes on tables that already exist, so an archive
    predating an ``sa.Index`` declaration needs it created explicitly.
    """
    kind = "UNIQUE INDEX" if unique else "INDEX"

    def step(con: sa.Connection) -> None:
        con.execute(sa.text(f"CREATE {kind} IF NOT EXISTS {name} ON {table}({columns})"))

    return step


def _documents_ingested_at(con: sa.Connection) -> None:
    """Add ``ingested_at``, backfilled from ``date_added`` for existing rows."""
    if "ingested_at" not in _columns(con, "documents"):
        con.execute(sa.text("ALTER TABLE documents ADD COLUMN ingested_at INTEGER"))
        con.execute(
            sa.text("UPDATE documents SET ingested_at = date_added WHERE ingested_at IS NULL")
        )


def _cluster_runs_status(con: sa.Connection) -> None:
    """Add ``status``; every run that predates it had finished."""
    cols = _columns(con, "cluster_runs")
    if cols and "status" not in cols:
        con.execute(sa.text("ALTER TABLE cluster_runs ADD COLUMN status TEXT DEFAULT 'finished'"))
        con.execute(sa.text("UPDATE cluster_runs SET status = 'finished' WHERE status IS NULL"))


def _overlay_tags_unique(con: sa.Connection) -> None:
    """Dedupe ``overlay_tags``, then enforce (document_id, tag, origin) uniqueness.

    The unique index cannot be created while duplicates exist, and archives
    that predate it may hold some.
    """
    con.execute(
        sa.text(
            "DELETE FROM overlay_tags WHERE id NOT IN ("
            " SELECT MIN(id) FROM overlay_tags GROUP BY document_id, tag, origin)"
        )
    )
    _create_index(
        "uq_overlay_doc_tag_origin", "overlay_tags", "document_id, tag, origin", unique=True
    )(con)


def _zotero_metadata_pass(con: sa.Connection) -> None:
    """Tag Zotero's existing title + abstract chunks ``pass='metadata'``.

    They were written untagged, and an untagged chunk counts as body text to
    ``rechunk`` and the ``fetched_text`` purge. Once Zotero also retains PDF
    text, a re-chunk would replace the abstract chunk with PDF blocks. Before
    the PDF pass existed every Zotero chunk was a title + abstract chunk, so
    every untagged one is tagged; ``fulltext`` chunks carry their own pass.
    """
    con.execute(
        sa.text(
            "UPDATE chunks SET chunk_pass = 'metadata' "
            "WHERE chunk_pass IS NULL AND document_id IN "
            "(SELECT id FROM documents WHERE source = 'zotero')"
        )
    )


MIGRATIONS: list[tuple[str, Step]] = [
    ("documents.ingested_at", _documents_ingested_at),
    # Cache generated summaries so a re-ingest never re-infers (DESIGN.md §3.2).
    ("documents.generated_summary", _add_column("documents", "generated_summary", "TEXT")),
    ("documents.zotero_attachment_key", _add_column("documents", "zotero_attachment_key", "TEXT")),
    ("documents.archive_url", _add_column("documents", "archive_url", "TEXT")),
    ("documents.item_type", _add_column("documents", "item_type", "TEXT")),
    ("documents.card_summary", _add_column("documents", "card_summary", "TEXT")),
    ("documents.note", _add_column("documents", "note", "TEXT")),
    ("documents.doc_embedding", _add_column("documents", "doc_embedding", "BLOB")),
    # Structured bibliographic fields (DESIGN.md §3.2).
    ("documents.doi", _add_column("documents", "doi", "TEXT")),
    ("documents.arxiv_id", _add_column("documents", "arxiv_id", "TEXT")),
    ("documents.isbn", _add_column("documents", "isbn", "TEXT")),
    ("documents.year", _add_column("documents", "year", "INTEGER")),
    ("documents.authors_json", _add_column("documents", "authors_json", "TEXT")),
    ("documents.zotero_url", _add_column("documents", "zotero_url", "TEXT")),
    ("documents.zotero_path", _add_column("documents", "zotero_path", "TEXT")),
    # Model provenance for the cached summary. Left NULL on existing rows: a
    # summary made before this shipped has genuinely unknown provenance, and
    # "whatever is configured now" would be a lie a purge would act on.
    ("documents.summary_run_id", _add_column("documents", "summary_run_id", "INTEGER")),
    # Enrichment provenance alongside each chunk, so the API can report which
    # rung of the ladder produced it (DESIGN.md §3.2). Pre-existing chunks keep
    # NULLs — there is no Chroma backfill.
    ("chunks.chunk_pass", _add_column("chunks", "chunk_pass", "TEXT")),
    ("chunks.resolved_by", _add_column("chunks", "resolved_by", "TEXT")),
    ("chunks.source_ref", _add_column("chunks", "source_ref", "TEXT")),
    ("chunks.ref_title", _add_column("chunks", "ref_title", "TEXT")),
    # Page range a chunk was read from, for PDF-backed sources.
    ("chunks.page_start", _add_column("chunks", "page_start", "INTEGER")),
    ("chunks.page_end", _add_column("chunks", "page_end", "INTEGER")),
    # How long the text was before a retention cap truncated it. Rows written
    # before the cap existed were stored whole, so NULL there reads as "not
    # truncated" — which is true, and not a guess.
    (
        "document_texts.full_char_count",
        _add_column("document_texts", "full_char_count", "INTEGER"),
    ),
    # Link images to their unified documents row.
    (
        "images.document_id",
        _add_column("images", "document_id", "INTEGER REFERENCES documents(id)"),
    ),
    # Cache the per-type cover extraction (DESIGN.md §3.2).
    ("images.books_json", _add_column("images", "books_json", "TEXT")),
    ("cluster_runs.umap_points", _add_column("cluster_runs", "umap_points", "TEXT")),
    ("cluster_runs.status", _cluster_runs_status),
    # Hierarchical clusters.
    ("clusters.level", _add_column("clusters", "level", "INTEGER NOT NULL DEFAULT 1")),
    (
        "clusters.parent_cluster_id",
        _add_column("clusters", "parent_cluster_id", "INTEGER REFERENCES clusters(cluster_id)"),
    ),
    ("clusters.centroid", _add_column("clusters", "centroid", "BLOB")),
    # Per-run noise bucket. Existing runs have no noise cluster, so every
    # pre-existing row is a real cluster (default 0).
    ("clusters.is_noise", _add_column("clusters", "is_noise", "BOOLEAN NOT NULL DEFAULT 0")),
    (
        "cluster_assignments.level",
        _add_column("cluster_assignments", "level", "INTEGER NOT NULL DEFAULT 1"),
    ),
    ("overlay_tags.uq_overlay_doc_tag_origin", _overlay_tags_unique),
    # The two columns the progress/status counts filter on.
    (
        "chunks.ix_chunks_document_id",
        _create_index("ix_chunks_document_id", "chunks", "document_id"),
    ),
    (
        "documents.ix_documents_source",
        _create_index("ix_documents_source", "documents", "source"),
    ),
    ("documents.ix_documents_doi", _create_index("ix_documents_doi", "documents", "doi")),
    (
        "documents.ix_documents_arxiv_id",
        _create_index("ix_documents_arxiv_id", "documents", "arxiv_id"),
    ),
    ("documents.ix_documents_isbn", _create_index("ix_documents_isbn", "documents", "isbn")),
    # Previously-unindexed foreign keys behind correlated EXISTS filters and
    # browse/search joins.
    (
        "source_tags.ix_source_tags_document_id_tag_string",
        _create_index(
            "ix_source_tags_document_id_tag_string", "source_tags", "document_id, tag_string"
        ),
    ),
    (
        "source_collections.ix_source_collections_document_id",
        _create_index("ix_source_collections_document_id", "source_collections", "document_id"),
    ),
    (
        "cluster_assignments.ix_cluster_assignments_run_id_document_id",
        _create_index(
            "ix_cluster_assignments_run_id_document_id",
            "cluster_assignments",
            "run_id, document_id",
        ),
    ),
    (
        "cluster_assignments.ix_cluster_assignments_run_id_cluster_id",
        _create_index(
            "ix_cluster_assignments_run_id_cluster_id",
            "cluster_assignments",
            "run_id, cluster_id",
        ),
    ),
    (
        "images.ix_images_document_id",
        _create_index("ix_images_document_id", "images", "document_id"),
    ),
    (
        "fetch_log.ix_fetch_log_document_id",
        _create_index("ix_fetch_log_document_id", "fetch_log", "document_id"),
    ),
    (
        "reading_list_items.ix_reading_list_items_list_id_document_id",
        _create_index(
            "ix_reading_list_items_list_id_document_id",
            "reading_list_items",
            "list_id, document_id",
        ),
    ),
    (
        "chunks.ix_chunks_document_id_chunk_index",
        _create_index("ix_chunks_document_id_chunk_index", "chunks", "document_id, chunk_index"),
    ),
    ("chunks.zotero_metadata_pass", _zotero_metadata_pass),
]


def init_db() -> None:
    """Create tables, then run every migration step this archive has not recorded."""
    eng = engine.get_engine()
    meta.create_all(eng)

    with eng.connect() as con:
        applied: set[str] = set(con.execute(sa.select(schema_migrations.c.name)).scalars())

    for name, step in MIGRATIONS:
        if name in applied:
            continue
        # One transaction per step: SQLite DDL is transactional, so a failing
        # step rolls back alone and leaves the steps before it recorded.
        with eng.begin() as con:
            step(con)
            con.execute(schema_migrations.insert().values(name=name, applied_at=int(time.time())))
