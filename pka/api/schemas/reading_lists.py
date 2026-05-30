"""Reading-list create/update/item models."""
from pydantic import BaseModel


class ListCreate(BaseModel):
    name: str
    description: str = ""


class ItemAdd(BaseModel):
    document_id: int
    note: str = ""
