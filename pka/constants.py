"""
Project-wide constants and string-based enums.

These enums are string-valued so existing DB rows (stored as text) remain
compatible. Callers can use either the enum member or the underlying string.
"""
from enum import StrEnum


class Source(StrEnum):
    FIREFOX = "firefox"
    ZOTERO = "zotero"
    CALIBRE = "calibre"
    IMAGE = "image"


class FetchStatus(StrEnum):
    PENDING = "pending"
    FETCHED = "fetched"
    UNFETCHABLE = "unfetchable"
    SKIPPED = "skipped"
    AVAILABLE = "available"   # Zotero/Calibre asset already on disk
    MISSING = "missing"       # Calibre book with no file


class TagOrigin(StrEnum):
    SOURCE = "source"
    INFERRED = "inferred"
    MANUAL = "manual"
    LLM = "llm"
    CLUSTER_L1 = "cluster_l1"
    CLUSTER_L2 = "cluster_l2"
    LEARNED = "learned"


ALL_SOURCES = [s.value for s in Source]
