"""
Read-only Zotero connector.

Operates on a temporary copy of ``zotero.sqlite`` to avoid lock contention
with a running Zotero process.

Dev (``PKA_DEV=1``): snapshot ``zotero.sqlite`` once into ``data/`` and reuse it.
Prod: fresh online backup on each connector access.
"""
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pka.config import settings
from pka.db.sqlite_copy import copy_sqlite_database, dev_sqlite_snapshot

log = logging.getLogger(__name__)


@dataclass
class ZoteroItem:
    source_id: str          # Zotero item key (8-char alphanumeric)
    title: str
    authors: list[str]
    abstract: str | None
    year: int | None
    doi: str | None
    url: str | None         # Zotero item URL field (article page, etc.)
    item_type: str          # journalArticle, book, webpage, ...
    collections: list[str]  # Zotero collection names
    tags: list[str]         # Zotero tag strings (verbatim)
    pdf_path: Path | None   # path to attached PDF, if any
    date_added: int | None  # unix timestamp
    highlight_text: str | None = None  # PDF highlight/comment (annotation items)
    pdf_attachment_key: str | None = None  # 8-char key of the PDF attachment item


def zotero_document_url_or_path(item: ZoteroItem) -> str | None:
    """Value stored on documents.url_or_path — prefer a browser-openable URL when present."""
    raw_url = (item.url or "").strip()
    if raw_url.lower().startswith(("http://", "https://")):
        return raw_url
    if item.pdf_path:
        return str(item.pdf_path)
    doi = (item.doi or "").strip()
    if doi:
        return doi
    if raw_url:
        return raw_url
    return None


def zotero_embed_text(item: ZoteroItem) -> str:
    """Build the text blob used for Zotero metadata embedding."""
    if item.item_type == "annotation":
        return (item.highlight_text or "").strip()
    parts = [item.title]
    if item.authors:
        parts.append("by " + ", ".join(item.authors))
    if item.abstract:
        parts.append(item.abstract)
    return "\n\n".join(p for p in parts if p)


# Fields we care about from itemData. Tuple — not set — to guarantee
# parameter ordering when bound to the SQL ``IN`` clause.
_FIELD_NAMES: tuple[str, ...] = ("title", "abstractNote", "DOI", "date", "url")


def ensure_zotero_copy(
    zotero_db: Path | None = None,
    copy_path: Path | None = None,
    *,
    refresh: bool = False,
) -> Path:
    """Copy ``zotero.sqlite`` for read-only access; return the copy path.

    When ``PKA_DEV=1``, the library DB is copied once to ``data/zotero_copy.sqlite``
    and that snapshot is reused. Pass ``refresh=True`` (or delete the copy) to resnapshot.
    """
    src = zotero_db or settings.zotero_db
    dst = copy_path or settings.zotero_db_copy
    if not src.exists():
        raise FileNotFoundError(f"Zotero database not found: {src}")
    if settings.dev:
        return dev_sqlite_snapshot(src, dst, label="Zotero", refresh=refresh)
    return copy_sqlite_database(src, dst)


def _parse_year(date_str: str | None) -> int | None:
    if not date_str:
        return None
    for part in date_str.split("-"):
        if part.isdigit() and len(part) == 4:
            return int(part)
    return None


def _table_exists(cur: sqlite3.Cursor, name: str) -> bool:
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return cur.fetchone() is not None


def _load_annotation_texts(cur: sqlite3.Cursor) -> dict[int, str]:
    """Map itemID → highlight/comment text for Zotero 7 annotation items."""
    if not _table_exists(cur, "itemAnnotations"):
        return {}
    cur.execute("SELECT itemID, text, comment FROM itemAnnotations")
    out: dict[int, str] = {}
    for item_id, text, comment in cur.fetchall():
        parts = [p for p in (text, comment) if p and str(p).strip()]
        if parts:
            out[item_id] = "\n\n".join(parts)
    return out


def _item_data_uses_value_id(cur: sqlite3.Cursor) -> bool:
    """Return True when itemData stores valueID (Zotero 7+) instead of value."""
    cur.execute("PRAGMA table_info(itemData)")
    cols = {row[1] for row in cur.fetchall()}
    return "valueID" in cols and "value" not in cols


def _load_item_fields(
    cur: sqlite3.Cursor,
    item_id: int,
    uses_value_id: bool,
) -> dict[str, str]:
    placeholders = ",".join("?" * len(_FIELD_NAMES))
    if uses_value_id:
        cur.execute(
            f"""
            SELECT f.fieldName, v.value
            FROM   itemData d
            JOIN   fieldsCombined f ON d.fieldID = f.fieldID
            JOIN   itemDataValues v ON d.valueID = v.valueID
            WHERE  d.itemID = ? AND f.fieldName IN ({placeholders})
            """,
            (item_id, *_FIELD_NAMES),
        )
    else:
        cur.execute(
            f"""
            SELECT f.fieldName, d.value
            FROM   itemData d
            JOIN   fields f ON d.fieldID = f.fieldID
            WHERE  d.itemID = ? AND f.fieldName IN ({placeholders})
            """,
            (item_id, *_FIELD_NAMES),
        )
    return {r["fieldName"]: r["value"] for r in cur.fetchall()}


_ITEM_KEYS_SQL = """
    SELECT i.key
    FROM   items i
    JOIN   itemTypes it ON i.itemTypeID = it.itemTypeID
    WHERE  it.typeName NOT IN ('attachment', 'note')
"""


def load_item_keys(
    zotero_db: Path | None = None,
    copy_path: Path | None = None,
    *,
    skip_copy: bool = False,
    refresh: bool = False,
) -> set[str]:
    """Return Zotero item keys without loading full item payloads."""
    dst = copy_path or settings.zotero_db_copy
    if not skip_copy:
        ensure_zotero_copy(zotero_db, dst, refresh=refresh)
    with sqlite3.connect(dst) as con:
        cur = con.cursor()
        cur.execute(_ITEM_KEYS_SQL)
        return {row[0] for row in cur.fetchall()}


def load_items(
    zotero_db: Path | None = None,
    copy_path: Path | None = None,
    *,
    keys: set[str] | None = None,
    skip_copy: bool = False,
    refresh: bool = False,
) -> list[ZoteroItem]:
    src = zotero_db or settings.zotero_db
    dst = copy_path or settings.zotero_db_copy

    if not skip_copy:
        ensure_zotero_copy(src, dst, refresh=refresh)
    elif not dst.exists():
        raise FileNotFoundError(f"Zotero copy not found: {dst}")

    if keys is not None and not keys:
        return []

    items: list[ZoteroItem] = []

    with sqlite3.connect(dst) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()

        cur.execute("""
            SELECT i.itemID, i.key, it.typeName, i.dateAdded
            FROM   items i
            JOIN   itemTypes it ON i.itemTypeID = it.itemTypeID
            WHERE  it.typeName NOT IN ('attachment', 'note')
        """)
        rows = cur.fetchall()
        if keys is not None:
            rows = [row for row in rows if row["key"] in keys]
        log.info("Loading %d Zotero items", len(rows))

        uses_value_id = _item_data_uses_value_id(cur)
        annotation_texts = _load_annotation_texts(cur)

        for row in rows:
            item_id  = row["itemID"]
            key      = row["key"]
            typeName = row["typeName"]

            try:
                dt = datetime.fromisoformat(row["dateAdded"])
                ts = int(dt.replace(tzinfo=timezone.utc).timestamp())
            except Exception:
                ts = None

            fields = _load_item_fields(cur, item_id, uses_value_id)

            cur.execute("""
                SELECT c.firstName, c.lastName
                FROM   itemCreators ic
                JOIN   creators c     ON ic.creatorID = c.creatorID
                JOIN   creatorTypes ct ON ic.creatorTypeID = ct.creatorTypeID
                WHERE  ic.itemID = ? AND ct.creatorType = 'author'
                ORDER  BY ic.orderIndex
            """, (item_id,))
            authors = [
                f"{r['firstName']} {r['lastName']}".strip()
                for r in cur.fetchall()
            ]

            cur.execute("""
                SELECT col.collectionName
                FROM   collectionItems ci
                JOIN   collections col ON ci.collectionID = col.collectionID
                WHERE  ci.itemID = ?
            """, (item_id,))
            collections = [r["collectionName"] for r in cur.fetchall()]

            cur.execute("""
                SELECT t.name
                FROM   itemTags it2
                JOIN   tags t ON it2.tagID = t.tagID
                WHERE  it2.itemID = ?
            """, (item_id,))
            tags = [r["name"] for r in cur.fetchall()]

            cur.execute("""
                SELECT ia.path, i2.key AS attachment_key
                FROM   itemAttachments ia
                JOIN   items i2 ON ia.itemID = i2.itemID
                WHERE  ia.parentItemID = ?
                  AND  ia.contentType = 'application/pdf'
                LIMIT  1
            """, (item_id,))
            att = cur.fetchone()
            pdf_path: Path | None = None
            pdf_attachment_key: str | None = None
            if att and att["path"]:
                pdf_attachment_key = att["attachment_key"]
                raw = att["path"]
                if raw.startswith("storage:"):
                    pdf_path = (
                        settings.zotero_db.parent
                        / "storage" / key / raw[len("storage:"):]
                    )
                else:
                    pdf_path = Path(raw)

            items.append(ZoteroItem(
                source_id   = key,
                title       = fields.get("title", ""),
                authors     = authors,
                abstract    = fields.get("abstractNote"),
                year        = _parse_year(fields.get("date")),
                doi         = fields.get("DOI"),
                url         = fields.get("url"),
                item_type   = typeName,
                collections = collections,
                tags        = tags,
                pdf_path    = pdf_path,
                pdf_attachment_key = pdf_attachment_key,
                date_added  = ts,
                highlight_text = annotation_texts.get(item_id),
            ))

    return items
