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
    "documents",
    meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("source", sa.Text, nullable=False),  # firefox|zotero|calibre|image
    sa.Column("source_id", sa.Text, nullable=False),  # id within the source system
    sa.Column("title", sa.Text),
    sa.Column("url_or_path", sa.Text),
    sa.Column("archive_url", sa.Text),  # Wayback snapshot when fetch used archive.org
    sa.Column("zotero_attachment_key", sa.Text),  # PDF attachment key for zotero://open-pdf
    sa.Column("date_added", sa.Integer),  # unix ts from source (when user saved it)
    sa.Column("ingested_at", sa.Integer),  # unix ts when Alexandria first indexed it
    sa.Column("fetch_status", sa.Text, default="pending"),  # see pka.constants.FetchStatus
    sa.Column("item_type", sa.Text),  # Zotero itemTypes.typeName
    sa.Column("card_summary", sa.Text),  # card excerpt (abstract, body lines, …)
    sa.Column("note", sa.Text),  # free-text notes (e.g. long Calibre tags)
    sa.Column("doc_embedding", sa.LargeBinary),  # mean-pooled float32 vector (384-d)
    sa.Column("generated_summary", sa.Text),  # cached LLM summary (DESIGN.md §3.2)
    # Structured bibliographic fields, cross-source (see DESIGN.md §3.2 and
    # planning/DOCUMENT_METADATA_PLAN.md). Nullable, populated by whoever has
    # the data — no per-source sidecar table.
    sa.Column("doi", sa.Text),  # bare DOI, lowercased, no doi.org/ prefix
    sa.Column("arxiv_id", sa.Text),  # normalize_arxiv_id form, no version suffix
    sa.Column("isbn", sa.Text),  # normalize_isbn form, digits/X, no hyphens
    sa.Column("year", sa.Integer),  # publication year, not date_added
    sa.Column("authors_json", sa.Text),  # JSON array of strings, order preserved
    sa.Column("zotero_url", sa.Text),  # Zotero item `url` field, verbatim
    sa.Column("zotero_path", sa.Text),  # resolved local attachment path
    sa.UniqueConstraint("source", "source_id", name="uq_source_item"),
    # Progress/status counts filter documents by source on every poll.
    sa.Index("ix_documents_source", "source"),
    # Join keys for cross-source dedup (planning/TODO.md).
    sa.Index("ix_documents_doi", "doi"),
    sa.Index("ix_documents_arxiv_id", "arxiv_id"),
    sa.Index("ix_documents_isbn", "isbn"),
)

source_tags = sa.Table(
    "source_tags",
    meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("tag_string", sa.Text, nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    # `_where_source_tag`'s correlated EXISTS and `_browse_tag_maps` both filter
    # by document_id and/or tag_string on every browse/tag-filtered query.
    sa.Index("ix_source_tags_document_id_tag_string", "document_id", "tag_string"),
)

source_collections = sa.Table(
    "source_collections",
    meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("collection", sa.Text, nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Index("ix_source_collections_document_id", "document_id"),
)

chunks = sa.Table(
    "chunks",
    meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("chunk_index", sa.Integer, nullable=False),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("token_count", sa.Integer),
    sa.Column("vector_id", sa.Text),  # Chroma document ID
    # Enrichment provenance mirrored from the Chroma chunk metadata so the API,
    # which serves documents from SQLite, can show which rung of the ladder
    # produced a chunk (DESIGN.md §3.2). The Chroma key is ``pass``, but
    # ``chunks.c.pass`` is a Python syntax error — hence ``chunk_pass``.
    sa.Column("chunk_pass", sa.Text),  # summary|external_synopsis|metadata|fulltext
    sa.Column("resolved_by", sa.Text),  # isbn|search|google_books|brave
    sa.Column("source_ref", sa.Text),  # ISBN or Open Library work key
    sa.Column("ref_title", sa.Text),  # resolved book title (shelf photos carry several)
    # Where in the source file the chunk came from, when the extractor can say:
    # real 1-based PDF page numbers, so a retrieved chunk can be cited back to
    # the pages it was read from. NULL for every non-paginated source.
    sa.Column("page_start", sa.Integer),
    sa.Column("page_end", sa.Integer),
    # Without this, "how many documents are embedded?" full-scans chunks.
    sa.Index("ix_chunks_document_id", "document_id"),
    # Lets `_batch_first_chunk_map`'s per-document MIN(chunk_index) run as an
    # index seek instead of sorting each document's chunks.
    sa.Index("ix_chunks_document_id_chunk_index", "document_id", "chunk_index"),
)

fetch_log = sa.Table(
    "fetch_log",
    meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("timestamp", sa.Integer, nullable=False),
    sa.Column("http_status", sa.Integer),
    sa.Column("error_msg", sa.Text),
    sa.Index("ix_fetch_log_document_id", "document_id"),
)

# Reddit-specific fields for a saved post or comment, keyed 1:1 to its document
# row (same shape as ``images``). Two of these cannot live on ``documents``:
#
#  * ``body`` — ``documents.card_summary`` holds a 280-char excerpt for cards, and
#    the chunks are overlapped and whitespace-normalised, so neither can give the
#    detail panel the post/comment as written. This is the verbatim text.
#  * ``permalink`` — a link post's ``documents.url_or_path`` is the *external*
#    target, which loses the reddit thread the user actually saved. Comments keep
#    theirs in ``url_or_path``; storing it here makes both kinds uniform.
reddit_items = sa.Table(
    "reddit_items",
    meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "document_id", sa.Integer, sa.ForeignKey("documents.id"), nullable=False, unique=True
    ),
    sa.Column("kind", sa.Text, nullable=False),  # post | comment
    sa.Column("subreddit", sa.Text),  # display name, no "r/" prefix
    sa.Column("permalink", sa.Text),  # canonical reddit thread URL
    sa.Column("external_url", sa.Text),  # link-post target, else NULL
    sa.Column("body", sa.Text),  # selftext / comment body, verbatim
)


# ── Overlay ──────────────────────────────────────────────────────────────────

overlay_tags = sa.Table(
    "overlay_tags",
    meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("tag", sa.Text, nullable=False),
    sa.Column("origin", sa.Text, nullable=False),  # see pka.constants.TagOrigin
    sa.Column("confidence", sa.Float),
    sa.Column("created_at", sa.Integer),
    sa.Index("uq_overlay_doc_tag_origin", "document_id", "tag", "origin", unique=True),
)

clusters = sa.Table(
    "clusters",
    meta,
    sa.Column("cluster_id", sa.Integer, primary_key=True),
    sa.Column("label", sa.Text),
    sa.Column("description", sa.Text),
    sa.Column("created_at", sa.Integer),
    sa.Column("run_id", sa.Integer, sa.ForeignKey("cluster_runs.run_id")),
    sa.Column("level", sa.Integer, nullable=False, server_default="1"),
    sa.Column(
        "parent_cluster_id",
        sa.Integer,
        sa.ForeignKey("clusters.cluster_id"),
    ),
    sa.Column("centroid", sa.LargeBinary),  # mean-pooled 384-d float32 blob
)

cluster_runs = sa.Table(
    "cluster_runs",
    meta,
    sa.Column("run_id", sa.Integer, primary_key=True),
    sa.Column("timestamp", sa.Integer, nullable=False),
    sa.Column("algorithm", sa.Text),
    sa.Column("parameters", sa.Text),  # JSON blob
    sa.Column("accepted", sa.Boolean, default=False),
    sa.Column("status", sa.Text, default="finished"),  # running|finished|failed|cancelled
    sa.Column("notes", sa.Text),
    sa.Column("umap_points", sa.Text),  # JSON: [{doc_id,x,y,cluster_id}]
)

cluster_assignments = sa.Table(
    "cluster_assignments",
    meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("cluster_id", sa.Integer, sa.ForeignKey("clusters.cluster_id"), nullable=False),
    sa.Column("run_id", sa.Integer, sa.ForeignKey("cluster_runs.run_id"), nullable=False),
    sa.Column("score", sa.Float),
    sa.Column("assigned_at", sa.Integer),
    sa.Column("level", sa.Integer, nullable=False, server_default="1"),
    # Every browse page and search join filters by run_id, often combined with
    # a document_id IN (...) list (`documents_out_batch`, `search.py`).
    sa.Index("ix_cluster_assignments_run_id_document_id", "run_id", "document_id"),
)

reading_lists = sa.Table(
    "reading_lists",
    meta,
    sa.Column("list_id", sa.Integer, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("created_at", sa.Integer),
)

reading_list_items = sa.Table(
    "reading_list_items",
    meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("list_id", sa.Integer, sa.ForeignKey("reading_lists.list_id"), nullable=False),
    sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("position", sa.Integer, default=0),
    sa.Column("note", sa.Text),
    sa.Index("ix_reading_list_items_list_id_document_id", "list_id", "document_id"),
)

# ── Images ───────────────────────────────────────────────────────────────────

images = sa.Table(
    "images",
    meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id")),  # unified document row
    sa.Column("path", sa.Text, nullable=False, unique=True),
    sa.Column("filename", sa.Text, nullable=False),
    sa.Column("image_type", sa.Text),  # see _VALID_TYPES in pka/ingestion/image_extractor.py
    sa.Column("width", sa.Integer),
    sa.Column("height", sa.Integer),
    sa.Column("file_size", sa.Integer),  # bytes
    sa.Column("date_taken", sa.Integer),  # unix ts from EXIF or mtime
    sa.Column("ocr_text", sa.Text),  # raw OCR output
    sa.Column("description", sa.Text),  # vision LLM prose description
    # Book fields the per-type cover prompt extracted (JSON list of
    # {title, authors, isbn}); cached so a later identifier lookup does not
    # re-run the VLM. See DESIGN.md §3.2.
    sa.Column("books_json", sa.Text),
    sa.Column("clip_vector_id", sa.Text),  # Chroma vector id for CLIP embedding
    sa.Column("text_vector_id", sa.Text),  # Chroma vector id for text embedding
    sa.Column("indexed_at", sa.Integer),
    # `_exclude_pending_images` runs a correlated EXISTS on this column for
    # every browse list and count query.
    sa.Index("ix_images_document_id", "document_id"),
)

image_tags = sa.Table(
    "image_tags",
    meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("image_id", sa.Integer, sa.ForeignKey("images.id"), nullable=False),
    sa.Column("tag", sa.Text, nullable=False),
    sa.Column("origin", sa.Text, nullable=False),  # see pka.constants.TagOrigin
)

# Images that failed the two-step admission gate (text coverage + VLM category).
# Cached by path so rejected images are skipped on subsequent ingestion runs.
image_rejections = sa.Table(
    "image_rejections",
    meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("path", sa.Text, nullable=False, unique=True),
    sa.Column("reason", sa.Text, nullable=False),  # low_text_coverage|not_category_of_interest
    sa.Column("text_coverage", sa.Float),  # measured fraction (0..1)
    sa.Column("image_type", sa.Text),  # gate classifier label
    sa.Column("rejected_at", sa.Integer),  # unix ts
)

# ── Tag training (active learning) ────────────────────────────────────────────

tag_training_sessions = sa.Table(
    "tag_training_sessions",
    meta,
    sa.Column("session_id", sa.Integer, primary_key=True),
    sa.Column("tag", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="labeling"),
    sa.Column("model_blob", sa.Text),
    sa.Column("parameters", sa.Text),
    sa.Column("provenance", sa.Text),
    sa.Column("notes", sa.Text),
    sa.Column("created_at", sa.Integer, nullable=False),
    sa.Column("accepted_at", sa.Integer),
)

tag_training_labels = sa.Table(
    "tag_training_labels",
    meta,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column(
        "session_id", sa.Integer, sa.ForeignKey("tag_training_sessions.session_id"), nullable=False
    ),
    sa.Column("document_id", sa.Integer, sa.ForeignKey("documents.id"), nullable=False),
    sa.Column("label", sa.Integer, nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("created_at", sa.Integer, nullable=False),
    sa.UniqueConstraint("session_id", "document_id", name="uq_tag_train_session_doc"),
)
