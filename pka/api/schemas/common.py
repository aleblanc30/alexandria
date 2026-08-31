"""Pagination + shared response wrappers."""

from pydantic import BaseModel


class Pagination(BaseModel):
    limit: int = 20
    offset: int = 0
    total: int
