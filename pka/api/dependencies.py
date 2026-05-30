"""Shared FastAPI dependencies — settings and DB engine."""
from pka.config import settings as _settings
from pka.db.queries import get_engine as _get_engine


def get_settings():
    return _settings


def get_engine():
    return _get_engine()
