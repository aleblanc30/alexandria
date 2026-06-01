"""Shared FastAPI dependencies — DB engine."""
from pka.db.queries import get_engine as _get_engine


def get_engine():
    return _get_engine()
