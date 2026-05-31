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
    date_added: int | None
    fetch_status: str
    source_tags: list[str]
    overlay_tags: list[TagOut]
    cluster_id: int | None
    cluster_label: str | None
    similarity: float | None = None   # only present in search results


class DocumentDetail(DocumentOut):
    chunks_count: int
    collections: list[str]


class TagPatchRequest(BaseModel):
    add: list[str] = []
    remove: list[str] = []


class DocumentListItem(BaseModel):
    id: int
    source: str
    title: str
    description: str


class DocumentListResponse(BaseModel):
    total: int
    documents: list[DocumentListItem]
