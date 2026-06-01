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


class DocumentDetail(DocumentOut):
    description: str = ""
    chunks_count: int
    collections: list[str]


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
