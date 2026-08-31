"""Tests for CLIP hit → ImageOut resolution (batched lookups)."""

import time

import sqlalchemy as sa

from pka.api.image_hits import clip_hits_to_image_out, image_tags_for
from pka.db.queries import get_engine, init_db
from pka.db.schema import image_tags, images


def _seed_images(n: int = 3) -> list[int]:
    init_db()
    now = int(time.time())
    ids = []
    with get_engine().begin() as con:
        for i in range(n):
            res = con.execute(
                images.insert().values(
                    path=f"/tmp/img{i}.png",
                    filename=f"img{i}.png",
                    image_type="slide",
                    width=800,
                    height=600,
                    file_size=1000,
                    date_taken=now,
                    description=f"description {i}",
                    ocr_text=None,
                    clip_vector_id=f"clip-{i}",
                )
            )
            image_id = res.inserted_primary_key[0]
            ids.append(image_id)
            con.execute(
                image_tags.insert().values(
                    image_id=image_id,
                    tag=f"tag-{i}",
                    origin="inferred",
                )
            )
    return ids


def test_hits_resolve_in_hit_order_with_tags():
    ids = _seed_images(3)
    hits = [
        {"vector_id": "clip-2", "distance": 0.1},
        {"vector_id": "clip-0", "distance": 0.3},
    ]
    with get_engine().connect() as con:
        out = clip_hits_to_image_out(con, hits)
    assert [o.id for o in out] == [ids[2], ids[0]]
    assert out[0].tags == ["tag-2"]
    assert out[1].tags == ["tag-0"]
    assert out[0].similarity == 0.9


def test_unknown_vector_id_is_skipped():
    _seed_images(1)
    hits = [
        {"vector_id": "clip-0", "distance": 0.2},
        {"vector_id": "clip-missing", "distance": 0.1},
    ]
    with get_engine().connect() as con:
        out = clip_hits_to_image_out(con, hits)
    assert len(out) == 1
    assert out[0].filename == "img0.png"


def test_round_similarity():
    _seed_images(1)
    hits = [{"vector_id": "clip-0", "distance": 0.123456}]
    with get_engine().connect() as con:
        out = clip_hits_to_image_out(con, hits, round_similarity=True)
    assert out[0].similarity == round(1.0 - 0.123456, 3)


def test_empty_hits():
    init_db()
    with get_engine().connect() as con:
        assert clip_hits_to_image_out(con, []) == []


def test_image_tags_for_single_image():
    ids = _seed_images(2)
    with get_engine().connect() as con:
        assert image_tags_for(con, ids[1]) == ["tag-1"]


def test_resolution_is_batched():
    """Two queries total (images + tags), regardless of hit count."""
    _seed_images(5)
    hits = [{"vector_id": f"clip-{i}", "distance": 0.1} for i in range(5)]
    statements: list[str] = []

    def count_stmt(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    eng = get_engine()
    sa.event.listen(eng, "before_cursor_execute", count_stmt)
    try:
        with eng.connect() as con:
            out = clip_hits_to_image_out(con, hits)
    finally:
        sa.event.remove(eng, "before_cursor_execute", count_stmt)
    assert len(out) == 5
    assert len(statements) <= 2
