"""Search request/response models. ``sources`` uses the :class:`Source` enum."""
from pydantic import BaseModel

from pka.api.schemas.documents import DocumentOut
from pka.api.schemas.images import ImageOut
from pka.constants import Source


class SearchRequest(BaseModel):
    query: str
    sources: list[Source] = []          # [] = all
    source_tags: list[str] = []
    general_tags: list[str] = []
    cluster_l1_tags: list[str] = []
    cluster_l2_tags: list[str] = []
    wayback_only: bool = False
    cluster_ids: list[int] = []
    tags: list[str] = []
    date_from: int | None = None
    date_to: int | None = None
    fetch_status: str | None = None
    mode: str = "semantic"              # semantic | fulltext | hybrid
    limit: int = 20
    offset: int = 0
    include_images: bool = False


class SearchResponse(BaseModel):
    query: str
    total: int
    documents: list[DocumentOut]
    images: list[ImageOut] = []
