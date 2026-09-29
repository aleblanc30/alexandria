"""Reddit saved items: the ``reddit_items`` side table."""

import sqlalchemy as sa

from pka.constants import Source
from pka.db import engine
from pka.db.schema import documents, reddit_items


def upsert_reddit_item(
    doc_id: int,
    *,
    kind: str,
    subreddit: str | None = None,
    permalink: str | None = None,
    external_url: str | None = None,
    body: str | None = None,
) -> None:
    """Store the Reddit-specific fields for *doc_id* (insert or update).

    Called on every pass over a saved item, not only the first, so a library
    ingested before this table existed gains its rows on the next metadata run
    without a dedicated backfill.
    """
    with engine.get_engine().begin() as con:
        existing = con.execute(
            sa.select(reddit_items.c.id).where(reddit_items.c.document_id == doc_id)
        ).fetchone()
        values = dict(
            kind=kind,
            subreddit=subreddit or None,
            permalink=permalink or None,
            external_url=external_url or None,
            body=body or None,
        )
        if existing:
            con.execute(
                sa.update(reddit_items).where(reddit_items.c.document_id == doc_id).values(**values)
            )
        else:
            con.execute(sa.insert(reddit_items).values(document_id=doc_id, **values))


def reddit_item(con: sa.Connection, doc_id: int) -> dict | None:
    """Reddit fields for *doc_id*, or ``None`` when the document has no row."""
    row = con.execute(
        sa.select(
            reddit_items.c.kind,
            reddit_items.c.subreddit,
            reddit_items.c.permalink,
            reddit_items.c.external_url,
            reddit_items.c.body,
        ).where(reddit_items.c.document_id == doc_id)
    ).fetchone()
    if not row:
        return None
    return {
        "kind": row[0],
        "subreddit": row[1],
        "permalink": row[2],
        "external_url": row[3],
        "body": row[4],
    }


def all_reddit_items() -> list[dict]:
    """Every persisted Reddit document joined with its ``reddit_items`` row.

    Feeds the ingest phase, which needs a body for every item still missing
    chunks: the metadata phase already persists (and refreshes, on every pass)
    kind/subreddit/permalink/external_url/body for each saved item, so this is
    the same data a second live feed poll would return, without the request.
    """
    with engine.get_engine().connect() as con:
        rows = con.execute(
            sa.select(
                documents.c.source_id,
                documents.c.title,
                documents.c.date_added,
                reddit_items.c.kind,
                reddit_items.c.subreddit,
                reddit_items.c.permalink,
                reddit_items.c.external_url,
                reddit_items.c.body,
            )
            .select_from(documents.join(reddit_items, reddit_items.c.document_id == documents.c.id))
            .where(documents.c.source == str(Source.REDDIT))
        ).fetchall()
    return [
        {
            "source_id": r[0],
            "title": r[1],
            "date_added": r[2],
            "kind": r[3],
            "subreddit": r[4],
            "permalink": r[5],
            "external_url": r[6],
            "body": r[7],
        }
        for r in rows
    ]
