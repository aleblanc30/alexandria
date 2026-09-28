"""Deprecated re-export shim for the helpers that used to live here.

``pka/db/queries.py`` was split by aggregate: :mod:`pka.db.engine`,
:mod:`pka.db.migrate`, :mod:`pka.db.documents`, :mod:`pka.db.chunks`,
:mod:`pka.db.cards`, :mod:`pka.db.clusters`, :mod:`pka.db.tags`,
:mod:`pka.db.browse`, :mod:`pka.db.reddit` and :mod:`pka.db.images`. Import
from those. This module only re-exports their public names so older imports
keep working.

Replacing a name here (``monkeypatch.setattr(queries, ...)``) no longer
reaches the code that uses it; patch the defining module instead.
"""

from pka.db.browse import apply_document_browse_filters, filter_document_ids, list_documents
from pka.db.cards import doc_title_excerpts, document_description, resolve_description
from pka.db.chunks import (
    ENRICHMENT_PASSES,
    document_enrichment,
    document_has_chunks,
    document_ids_with_chunks,
    existing_chunk_count,
    insert_chunks,
    source_ids_with_chunks,
)
from pka.db.clusters import sample_cluster_documents, sample_cluster_documents_for_clusters
from pka.db.documents import (
    DocumentWrite,
    document_index,
    document_titles,
    firefox_ingest_queue,
    get_generated_summary,
    insert_document_if_new,
    refresh_zotero_metadata,
    set_fetch_status,
    set_generated_summary,
    source_ingest_queue,
    update_card_summary,
    update_document_item_type,
    upsert_document,
)
from pka.db.engine import get_engine
from pka.db.images import (
    clear_image_rejections,
    delete_image_document,
    get_rejected_paths,
    record_image_rejection,
)
from pka.db.migrate import init_db
from pka.db.reddit import all_reddit_items, reddit_item, upsert_reddit_item
from pka.db.tags import insert_source_collections, insert_source_tags, list_tags

__all__ = [
    "ENRICHMENT_PASSES",
    "DocumentWrite",
    "all_reddit_items",
    "apply_document_browse_filters",
    "clear_image_rejections",
    "delete_image_document",
    "doc_title_excerpts",
    "document_description",
    "document_enrichment",
    "document_has_chunks",
    "document_ids_with_chunks",
    "document_index",
    "document_titles",
    "existing_chunk_count",
    "filter_document_ids",
    "firefox_ingest_queue",
    "get_engine",
    "get_generated_summary",
    "get_rejected_paths",
    "init_db",
    "insert_chunks",
    "insert_document_if_new",
    "insert_source_collections",
    "insert_source_tags",
    "list_documents",
    "list_tags",
    "record_image_rejection",
    "reddit_item",
    "refresh_zotero_metadata",
    "resolve_description",
    "sample_cluster_documents",
    "sample_cluster_documents_for_clusters",
    "set_fetch_status",
    "set_generated_summary",
    "source_ids_with_chunks",
    "source_ingest_queue",
    "update_card_summary",
    "update_document_item_type",
    "upsert_document",
    "upsert_reddit_item",
]
