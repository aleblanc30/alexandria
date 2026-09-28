"""Verbatim retention of extracted body text (``document_texts``).

Why this exists: after ingestion the
body text survives only as ``chunks.text``, which is whitespace-normalised, cut
into overlapping chunks, and missing every chunk shorter than
``min_chunk_chars``. Re-summarising, re-chunking, re-running an extraction fix
or auditing what the fetcher actually got therefore all require going back to
the network. Keeping the text costs a little disk and retires that.

**What gets stored** is text with no other verbatim home that cost a network
round trip or a slow extraction to produce: fetched bodies, and (later) book /
PDF extractions. Reddit's inline bodies (``reddit_items.body``), image OCR and
descriptions (``images``), Zotero abstracts and YouTube descriptions are all
either already retained or a millisecond re-read from their own source, so they
stay out — duplicating them would be disk spent to save nothing.

Deliberately *not* called from :func:`pka.ingestion.core.ingest_text_block`:
that runs once per block (a book calls it per section) and once per pass (the
generated summary goes through it too), so a write there would overwrite a body
with a summary. Runners call this explicitly, which keeps the invariant plain:
**one row is one document's body text.**
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import zlib

import sqlalchemy as sa

from pka.config import settings as cfg
from pka.db.engine import get_engine
from pka.db.schema import document_texts

log = logging.getLogger(__name__)

#: Value of ``document_texts.encoding`` this module writes. The column exists so
#: a later codec change is a migration rather than an archaeology exercise.
ENCODING = "zlib"

_COMPRESS_LEVEL = 6


def encode_text(text: str) -> bytes:
    return zlib.compress(text.encode("utf-8"), _COMPRESS_LEVEL)


def decode_text(blob: bytes, encoding: str = ENCODING) -> str:
    if encoding == ENCODING:
        return zlib.decompress(blob).decode("utf-8")
    if encoding == "raw":
        return bytes(blob).decode("utf-8")
    raise ValueError(f"Unknown document_texts encoding: {encoding!r}")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def section_blocks(sections: list[dict]) -> tuple[str, list[dict]]:
    """Join extracted sections into one body, and map each back into it.

    Returns ``(text, blocks)`` where every block carries the ``offset`` and
    ``length`` that slice its section out of ``text`` verbatim. Without that map
    a re-chunk of a book could not reproduce the ``section_title`` /
    ``section_index`` / ``page_start`` / ``page_end`` metadata the chunks
    carried, so it would be a strict downgrade of what it replaced.

    Empty sections are dropped and each text is stripped: the joined string then
    needs no further normalisation, and since :func:`store_document_text` strips
    what it is given, an offset computed against an unstripped join would be off
    by exactly the whitespace it removed.
    """
    parts: list[str] = []
    blocks: list[dict] = []
    offset = 0
    for section in sections:
        text = (section.get("text") or "").strip()
        if not text:
            continue
        blocks.append(
            {
                "index": section.get("index", len(blocks)),
                "title": section.get("title") or "",
                "page_start": section.get("page_start"),
                "page_end": section.get("page_end"),
                "offset": offset,
                "length": len(text),
            }
        )
        parts.append(text)
        offset += len(text) + 2  # the "\n\n" the join inserts after this part
    return "\n\n".join(parts), blocks


def truncate_blocks(
    text: str,
    blocks: list[dict],
    *,
    max_pages: int | None = None,
    max_chars: int | None = None,
) -> tuple[str, list[dict]]:
    """Keep the opening of a sectioned text, cut on a section boundary.

    The cap that makes retention affordable for books: a 600-page PDF is the one
    input that can make the sidecar cost real disk, and the value of retaining
    it falls off a cliff after the opening — enough to summarise, to audit what
    the extractor produced, and to search. Fetched pages are *not* capped: they
    are small, and they are the ones that cannot be re-read from disk.

    Whole sections only, so every surviving block's ``offset``/``length`` still
    slices the returned text exactly. ``max_pages`` is compared against a
    section's ``page_start`` (1-based), so "20 pages" means what it says for a
    PDF; ``max_chars`` is the equivalent for EPUB chapters, which carry no page
    numbers. The first section is always kept, however long it is — a cap that
    can return nothing would silently retain no text at all.
    """
    if not blocks:
        return text, blocks
    kept: list[dict] = []
    used = 0
    for block in blocks:
        if kept:
            page_start = block.get("page_start")
            if max_pages is not None and page_start is not None and page_start > max_pages:
                break
            if max_chars is not None and used + block.get("length", 0) > max_chars:
                break
        kept.append(block)
        used = block.get("offset", 0) + block.get("length", 0)
    if len(kept) == len(blocks):
        return text, blocks
    return text[:used], kept


def store_document_text(
    doc_id: int,
    text: str,
    *,
    blocks: list[dict] | None = None,
    full_char_count: int | None = None,
    dry_run: bool = False,
) -> bool:
    """Retain *text* as ``doc_id``'s body text. Returns whether a row was written.

    Idempotent: a second call for the same document replaces the row, so a
    re-fetch refreshes the text (and its hash) rather than duplicating it.

    ``full_char_count`` is the length *before* a caller truncated the text (see
    :func:`truncate_blocks`); it defaults to the length of what is stored, which
    is the honest answer whenever nothing was cut. What it buys is that a reader
    can tell a prefix from a whole document — the re-chunk pass refuses to work
    from a prefix, because re-cutting one would silently shrink the index.

    Never raises. Retention is a convenience for later passes, not part of what
    makes a document ingested — a failure here must not cost the document its
    chunks, the same rule :func:`pka.ingestion.core.attach_summary_chunk`
    follows for summaries.
    """
    if dry_run or not cfg.retain_document_text:
        return False
    body = (text or "").strip()
    if not body:
        return False
    try:
        values = {
            "text": encode_text(body),
            "encoding": ENCODING,
            "char_count": len(body),
            "full_char_count": max(full_char_count or 0, len(body)),
            "content_hash": content_hash(body),
            "blocks_json": json.dumps(blocks) if blocks else None,
            "extracted_at": int(time.time()),
        }
        with get_engine().begin() as con:
            existing = con.execute(
                sa.select(document_texts.c.id).where(document_texts.c.document_id == doc_id)
            ).fetchone()
            if existing:
                con.execute(
                    sa.update(document_texts)
                    .where(document_texts.c.document_id == doc_id)
                    .values(**values)
                )
            else:
                con.execute(sa.insert(document_texts).values(document_id=doc_id, **values))
        return True
    except Exception:
        log.exception("Storing document text failed for doc_id=%d", doc_id)
        return False


def load_document_text(doc_id: int) -> str | None:
    """The retained body text for *doc_id*, or ``None`` when there is none.

    ``None`` is the ordinary case for anything ingested before retention
    shipped: there is no backfill, and reconstructing text from chunks would
    look verbatim while being a reconstruction.
    """
    with get_engine().connect() as con:
        row = con.execute(
            sa.select(document_texts.c.text, document_texts.c.encoding).where(
                document_texts.c.document_id == doc_id
            )
        ).fetchone()
    if not row:
        return None
    try:
        return decode_text(row[0], row[1] or ENCODING)
    except Exception:
        log.exception("Decoding stored text failed for doc_id=%d", doc_id)
        return None


def document_text_meta(doc_id: int) -> dict | None:
    """Size, hash, section map and timestamp for *doc_id*, without decompressing.

    ``truncated`` says whether this row is a prefix of the document rather than
    the whole of it. A row written before the retention cap existed has a NULL
    ``full_char_count`` and reads as untruncated, which is what it is.
    """
    with get_engine().connect() as con:
        row = con.execute(
            sa.select(
                document_texts.c.char_count,
                document_texts.c.content_hash,
                document_texts.c.blocks_json,
                document_texts.c.extracted_at,
                document_texts.c.encoding,
                document_texts.c.full_char_count,
            ).where(document_texts.c.document_id == doc_id)
        ).fetchone()
    if not row:
        return None
    char_count, full_char_count = row[0], row[5]
    return {
        "char_count": char_count,
        "full_char_count": full_char_count if full_char_count is not None else char_count,
        "truncated": bool(
            full_char_count is not None and char_count is not None and full_char_count > char_count
        ),
        "content_hash": row[1],
        "blocks": json.loads(row[2]) if row[2] else None,
        "extracted_at": row[3],
        "encoding": row[4],
    }
