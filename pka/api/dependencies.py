"""Shared FastAPI dependencies — DB engine."""

from pka.db.engine import get_engine as _get_engine


def get_engine():
    return _get_engine()
