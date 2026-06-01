"""Apply cluster labels as overlay tags on documents."""
from __future__ import annotations

import re
import time

import sqlalchemy as sa

from pka.constants import TagOrigin
from pka.db.schema import cluster_assignments, overlay_tags

_SLUG_RE = re.compile(r"[^\w\s-]", re.UNICODE)
_SPACE_RE = re.compile(r"[\s_]+")
_HYPHEN_RE = re.compile(r"-+")


def slugify_tag(text: str, max_len: int = 64) -> str:
    """Normalize free text into a lowercase hyphenated tag string."""
    s = _SLUG_RE.sub("", text.lower().strip())
    s = _HYPHEN_RE.sub("-", _SPACE_RE.sub("-", s)).strip("-")
    return s[:max_len]


def label_to_tag(label: str | None, cluster_id: int) -> str:
    """Derive overlay tag from cluster display label."""
    return slugify_tag(label or "") or f"cluster-{cluster_id}"


def cluster_document_ids(con, cluster_id: int, run_id: int) -> list[int]:
    return [
        r[0]
        for r in con.execute(
            sa.select(cluster_assignments.c.document_id).where(
                (cluster_assignments.c.cluster_id == cluster_id)
                & (cluster_assignments.c.run_id == run_id)
            )
        ).fetchall()
    ]


def apply_tag_to_documents(
    con,
    doc_ids: list[int],
    tag: str,
    origin: TagOrigin = TagOrigin.LLM,
) -> tuple[int, int]:
    """Insert overlay tag for each document. Returns (applied, skipped)."""
    tag = slugify_tag(tag) or tag.strip()
    if not tag or not doc_ids:
        return 0, 0

    now = int(time.time())
    applied = skipped = 0
    for doc_id in doc_ids:
        existing = con.execute(
            sa.select(sa.func.count())
            .select_from(overlay_tags)
            .where(
                (overlay_tags.c.document_id == doc_id)
                & (overlay_tags.c.tag == tag)
                & (overlay_tags.c.origin == str(origin))
            )
        ).scalar()
        if existing:
            skipped += 1
            continue
        con.execute(
            sa.text("""
                INSERT INTO overlay_tags
                    (document_id, tag, origin, created_at)
                VALUES (:did, :tag, :origin, :now)
            """),
            {
                "did": doc_id,
                "tag": tag,
                "origin": str(origin),
                "now": now,
            },
        )
        applied += 1
    return applied, skipped


def top_tags_for_cluster(con, cluster_id: int, run_id: int, limit: int = 10) -> list[str]:
    """Most common source tags on documents in this cluster."""
    from pka.db.schema import source_tags

    doc_ids = cluster_document_ids(con, cluster_id, run_id)[:200]
    if not doc_ids:
        return []
    rows = con.execute(
        sa.select(source_tags.c.tag_string, sa.func.count().label("n"))
        .where(source_tags.c.document_id.in_(doc_ids))
        .group_by(source_tags.c.tag_string)
        .order_by(sa.desc("n"))
        .limit(limit)
    ).fetchall()
    return [r[0] for r in rows]
