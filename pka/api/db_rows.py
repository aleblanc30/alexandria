"""SQLAlchemy 2.x row helpers.

Full-table ``Row`` objects support attribute access (``row.id``) but not
string indexing (``row["id"]``).  Use ``.mappings()`` for dict-like access.
"""
from collections.abc import Mapping
from typing import Any

from sqlalchemy.engine import CursorResult


def fetchall_mappings(result: CursorResult[Any]) -> list[Mapping[str, Any]]:
    return list(result.mappings().all())


def fetchone_mapping(result: CursorResult[Any]) -> Mapping[str, Any] | None:
    return result.mappings().first()
