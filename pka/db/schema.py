"""
SQLAlchemy Core table definitions for ``archive.db``.

Two namespaces:
  - core tables   : populated from source connectors, never edited by user
  - overlay tables: system-derived and user-editable
"""
import sqlalchemy as sa

meta = sa.MetaData()

# ── Core ────────────────────────────────────────────────────────────────────

documents = sa.Table(
    "documents", meta,
    sa.Column("id",           sa.Integer, primary_key=True),
    sa.Column("source",       sa.Text, nullable=False),    # firefox|zotero|calibre|image
    sa.Column("source_id",    sa.Text, nullable=False),    # id within the source system
    sa.Column("title",        sa.Text),
    sa.Column("url_or_path",  sa.Text),
    sa.Column("archive_url",  sa.Text),                  # Wayback snapshot when fetch used archive.org
    sa.Column("zotero_attachment_key", sa.Text),           # PDF attachment key for zotero://open-pdf
    sa.Column("date_added",   sa.Integer),                 # unix ts from source (when user saved it)
    sa.Column("ingested_at",  sa.Integer),                 # unix ts when PKA first indexed it
    sa.Column("fetch_status", sa.Text, default="pending"), # see pka.constants.FetchStatus
    sa.Column("item_type",    sa.Text),                    # Zotero itemTypes.typeName
    sa.Column("card_summary", sa.Text),                    # card excerpt (abstract, body lines, …)
    sa.Column("doc_embedding", sa.LargeBinary),            # mean-pooled float32 vector (384-d)
    sa.UniqueConstraint("source", "source_id", name="uq_source_item"),
)

source_tags = sa.Table(
    "source_tags", meta,
    sa.Column("id",          sa.Integer, primary_key=True),
    sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("tag_string",  sa.Text, nullable=False),
    sa.Column("source",      sa.Text, nullable=False),
)

source_collections = sa.Table(
    "source_collections", meta,
    sa.Column("id",          sa.Integer, primary_key=True),
    sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("collection",  sa.Text, nullable=False),
    sa.Column("source",      sa.Text, nullable=False),
)

chunks = sa.Table(
    "chunks", meta,
    sa.Column("id",            sa.Integer, primary_key=True),
    sa.Column("document_id",   sa.Integer, sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("chunk_index",   sa.Integer, nullable=False),
    sa.Column("text",          sa.Text, nullable=False),
    sa.Column("token_count",   sa.Integer),
    sa.Column("vector_id",     sa.Text),                   # Chroma document ID
)

fetch_log = sa.Table(
    "fetch_log", meta,
    sa.Column("id",          sa.Integer, primary_key=True),
    sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("timestamp",   sa.Integer, nullable=False),
    sa.Column("http_status", sa.Integer),
    sa.Column("error_msg",   sa.Text),
)

# ── Overlay ──────────────────────────────────────────────────────────────────

overlay_tags = sa.Table(
    "overlay_tags", meta,
    sa.Column("id",          sa.Integer, primary_key=True),
    sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("tag",         sa.Text, nullable=False),
    sa.Column("origin",      sa.Text, nullable=False),     # see pka.constants.TagOrigin
    sa.Column("confidence",  sa.Float),
    sa.Column("created_at",  sa.Integer),
)

clusters = sa.Table(
    "clusters", meta,
    sa.Column("cluster_id",  sa.Integer, primary_key=True),
    sa.Column("label",       sa.Text),
    sa.Column("description", sa.Text),
    sa.Column("created_at",  sa.Integer),
    sa.Column("run_id",      sa.Integer, sa.ForeignKey("cluster_runs.run_id")),
    sa.Column("level",       sa.Integer, nullable=False, server_default="1"),
    sa.Column(
        "parent_cluster_id",
        sa.Integer,
        sa.ForeignKey("clusters.cluster_id"),
    ),
)

cluster_runs = sa.Table(
    "cluster_runs", meta,
    sa.Column("run_id",     sa.Integer, primary_key=True),
    sa.Column("timestamp",  sa.Integer, nullable=False),
    sa.Column("algorithm",  sa.Text),
    sa.Column("parameters", sa.Text),                       # JSON blob
    sa.Column("accepted",   sa.Boolean, default=False),
    sa.Column("status",     sa.Text, default="finished"),    # running|finished|failed|cancelled
    sa.Column("notes",      sa.Text),
    sa.Column("umap_points", sa.Text),                      # JSON: [{doc_id,x,y,cluster_id}]
)

cluster_assignments = sa.Table(
    "cluster_assignments", meta,
    sa.Column("id",          sa.Integer, primary_key=True),
    sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("cluster_id",  sa.Integer, sa.ForeignKey("clusters.cluster_id"), nullable=False),
    sa.Column("run_id",      sa.Integer, sa.ForeignKey("cluster_runs.run_id"), nullable=False),
    sa.Column("score",       sa.Float),
    sa.Column("assigned_at", sa.Integer),
    sa.Column("level",       sa.Integer, nullable=False, server_default="1"),
)

reading_lists = sa.Table(
    "reading_lists", meta,
    sa.Column("list_id",     sa.Integer, primary_key=True),
    sa.Column("name",        sa.Text, nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("created_at",  sa.Integer),
)

reading_list_items = sa.Table(
    "reading_list_items", meta,
    sa.Column("id",          sa.Integer, primary_key=True),
    sa.Column("list_id",     sa.Integer, sa.ForeignKey("reading_lists.list_id"), nullable=False),
    sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("position",    sa.Integer, default=0),
    sa.Column("note",        sa.Text),
)

# ── Images ───────────────────────────────────────────────────────────────────

images = sa.Table(
    "images", meta,
    sa.Column("id",             sa.Integer, primary_key=True),
    sa.Column("path",           sa.Text, nullable=False, unique=True),
    sa.Column("filename",       sa.Text, nullable=False),
    sa.Column("image_type",     sa.Text),                  # book_cover|slide|poster|notes|whiteboard|unknown
    sa.Column("width",          sa.Integer),
    sa.Column("height",         sa.Integer),
    sa.Column("file_size",      sa.Integer),               # bytes
    sa.Column("date_taken",     sa.Integer),               # unix ts from EXIF or mtime
    sa.Column("ocr_text",       sa.Text),                  # raw Tesseract output
    sa.Column("description",    sa.Text),                  # vision LLM prose description
    sa.Column("clip_vector_id", sa.Text),                  # Chroma vector id for CLIP embedding
    sa.Column("text_vector_id", sa.Text),                  # Chroma vector id for text embedding
    sa.Column("indexed_at",     sa.Integer),
)

image_tags = sa.Table(
    "image_tags", meta,
    sa.Column("id",       sa.Integer, primary_key=True),
    sa.Column("image_id", sa.Integer, sa.ForeignKey("images.id"), nullable=False),
    sa.Column("tag",      sa.Text, nullable=False),
    sa.Column("origin",   sa.Text, nullable=False),        # see pka.constants.TagOrigin
)
