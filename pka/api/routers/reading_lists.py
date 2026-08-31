"""``/reading-lists`` — CRUD over the curated reading-list overlay."""

import time

import sqlalchemy as sa
from fastapi import APIRouter, Depends

from pka.api.db_rows import fetchall_mappings
from pka.api.dependencies import get_engine
from pka.api.schemas.reading_lists import ItemAdd, ListCreate
from pka.db.schema import documents, reading_list_items, reading_lists

router = APIRouter(prefix="/reading-lists", tags=["reading_lists"])


@router.get("")
def list_reading_lists(engine=Depends(get_engine)):
    with engine.connect() as con:
        rows = fetchall_mappings(con.execute(sa.select(reading_lists)))
        out = []
        for r in rows:
            n = con.execute(
                sa.select(sa.func.count())
                .select_from(reading_list_items)
                .where(reading_list_items.c.list_id == r["list_id"])
            ).scalar()
            out.append(
                {
                    "list_id": r["list_id"],
                    "name": r["name"],
                    "description": r["description"],
                    "created_at": r["created_at"],
                    "item_count": n,
                }
            )
    return out


@router.post("", status_code=201)
def create_list(body: ListCreate, engine=Depends(get_engine)):
    with engine.begin() as con:
        res = con.execute(
            reading_lists.insert().values(
                name=body.name,
                description=body.description,
                created_at=int(time.time()),
            )
        )
    return {"list_id": res.inserted_primary_key[0]}


@router.get("/{list_id}/items")
def list_items(list_id: int, engine=Depends(get_engine)):
    with engine.connect() as con:
        rows = con.execute(
            sa.select(
                reading_list_items.c.id,
                reading_list_items.c.position,
                reading_list_items.c.note,
                documents.c.id.label("doc_id"),
                documents.c.title,
                documents.c.source,
                documents.c.url_or_path,
            )
            .join(documents, documents.c.id == reading_list_items.c.document_id)
            .where(reading_list_items.c.list_id == list_id)
            .order_by(reading_list_items.c.position)
        ).fetchall()
    return [
        {
            "id": r[0],
            "position": r[1],
            "note": r[2],
            "doc_id": r[3],
            "title": r[4],
            "source": r[5],
            "url_or_path": r[6],
        }
        for r in rows
    ]


@router.post("/{list_id}/items", status_code=201)
def add_item(list_id: int, body: ItemAdd, engine=Depends(get_engine)):
    with engine.begin() as con:
        max_pos = con.execute(
            sa.select(sa.func.coalesce(sa.func.max(reading_list_items.c.position), 0)).where(
                reading_list_items.c.list_id == list_id
            )
        ).scalar()
        res = con.execute(
            reading_list_items.insert().values(
                list_id=list_id,
                document_id=body.document_id,
                position=max_pos + 1,
                note=body.note,
            )
        )
    return {"id": res.inserted_primary_key[0]}


@router.delete("/{list_id}/items/{item_id}", status_code=204)
def remove_item(list_id: int, item_id: int, engine=Depends(get_engine)):
    with engine.begin() as con:
        con.execute(
            reading_list_items.delete().where(
                (reading_list_items.c.id == item_id) & (reading_list_items.c.list_id == list_id)
            )
        )


@router.delete("/{list_id}", status_code=204)
def delete_list(list_id: int, engine=Depends(get_engine)):
    with engine.begin() as con:
        con.execute(reading_list_items.delete().where(reading_list_items.c.list_id == list_id))
        con.execute(reading_lists.delete().where(reading_lists.c.list_id == list_id))
