"""Document, tag, and document-detail response models."""
from pydantic import BaseModel


class TagOut(BaseModel):
    tag: str
    origin: str   # source | inferred | manual | llm
    confidence: float | None = None


class DocumentOut(BaseModel):
    id: int
    source: str
    source_id: str
    title: str
    url_or_path: str | None
    archive_url: str | None = None
    zotero_attachment_key: str | None = None
    date_added: int | None
    fetch_status: str
    source_tags: list[str]
    overlay_tags: list[TagOut]
    cluster_id: int | None
    cluster_label: str | None
    similarity: float | None = None   # only present in search results
    description: str = ""
    note: str | None = None
    # Structured bibliographic fields (DESIGN.md §3.2, DOCUMENT_METADATA_PLAN.md).
    doi: str | None = None
    doi_url: str | None = None        # https://doi.org/<doi>, computed
    arxiv_id: str | None = None
    isbn: str | None = None
    year: int | None = None           # publication year, not date_added
    authors: list[str] = []


class ImageDetail(BaseModel):
    """Image-specific fields, present only when ``source == image``."""
    image_type: str | None = None
    ocr_text: str | None = None


class RedditDetail(BaseModel):
    """Reddit-specific fields, present only when ``source == reddit``.

    ``body`` is the post or comment verbatim, not the 280-char ``description``
    the cards use. ``permalink`` is the reddit thread; for a link post that is a
    different URL from ``url_or_path``, which points at the external target.
    """
    kind: str | None = None            # post | comment
    subreddit: str | None = None       # display name, no "r/" prefix
    permalink: str | None = None
    external_url: str | None = None    # link-post target, else None
    body: str | None = None


class EnrichmentOut(BaseModel):
    """One retrieval-enrichment chunk's provenance (DESIGN.md §3.2).

    ``label`` is rendered here rather than in the frontend so the enrichment
    ladder stays the single source of truth for how each rung is named.
    """
    kind: str                  # summary | external_synopsis
    resolved_by: str | None    # isbn|search|google_books|brave|local_model
    label: str                 # human-readable, e.g. "Open Library · ISBN"
    source_ref: str | None     # ISBN or Open Library work key
    ref_title: str | None      # resolved book title
    text: str


class DocumentDetail(DocumentOut):
    description: str = ""
    chunks_count: int
    collections: list[str]
    image: ImageDetail | None = None
    reddit: RedditDetail | None = None
    enrichment: list[EnrichmentOut] = []


class TagPatchRequest(BaseModel):
    add: list[str] = []
    remove: list[str] = []


class DocumentListItem(BaseModel):
    id: int
    source: str
    source_id: str
    title: str
    description: str
    url_or_path: str | None = None
    archive_url: str | None = None
    zotero_attachment_key: str | None = None
    source_tags: list[str] = []
    cluster_l1_tags: list[str] = []
    cluster_l2_tags: list[str] = []


class DocumentListResponse(BaseModel):
    total: int
    documents: list[DocumentListItem]
