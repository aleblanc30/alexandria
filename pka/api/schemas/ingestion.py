"""Per-source ingestion path models."""

from pydantic import BaseModel


class SourcePathUpdate(BaseModel):
    path: str
