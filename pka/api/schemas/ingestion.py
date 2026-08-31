"""Per-source ingestion path models."""

from pydantic import BaseModel


class SourcePathUpdate(BaseModel):
    path: str


class DomainRow(BaseModel):
    domain: str
    count: int
    unfetchable: int
    has_handler: bool
    by_fetch_status: dict[str, int]


class DomainTopLists(BaseModel):
    top_domains: list[DomainRow]
    top_unfetchable: list[DomainRow]
