"""``/clusters`` — list, detail, document membership, and 2-D scatter layout."""

import json
import time

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException

from pka.api.active_run import fetch_active_run_id
from pka.api.db_rows import fetchall_mappings, fetchone_mapping
from pka.api.dependencies import get_engine
from pka.api.schemas.clusters import (
    ApplyAllTagsResult,
    ApplyTagRequest,
    ApplyTagResult,
    ClusterDetail,
    ClusterOut,
    ClusterPatchRequest,
    UmapPoint,
)
from pka.clustering.cluster_tags import (
    apply_tag_to_documents,
    cluster_document_ids,
    label_to_tag,
    top_tags_for_cluster,
)
from pka.clustering.engine import relabel_single_cluster
from pka.constants import TagOrigin
from pka.db.schema import (
    cluster_assignments,
    cluster_runs,
    clusters,
    documents,
)

router = APIRouter(prefix="/clusters", tags=["clusters"])


def _cluster_tag_origin(level: int) -> TagOrigin:
    return TagOrigin.CLUSTER_L2 if level == 2 else TagOrigin.CLUSTER_L1


def _parent_label_map(con, run_id: int) -> dict[int, str]:
    rows = con.execute(
        sa.select(clusters.c.cluster_id, clusters.c.label).where(clusters.c.run_id == run_id)
    ).fetchall()
    return {r[0]: r[1] or "" for r in rows}


def _cluster_doc_count(con, cluster_id: int, run_id: int) -> int:
    return (
        con.execute(
            sa.select(sa.func.count())
            .select_from(cluster_assignments)
            .where(
                (cluster_assignments.c.cluster_id == cluster_id)
                & (cluster_assignments.c.run_id == run_id)
            )
        ).scalar()
        or 0
    )


def _cluster_out_by_id(con, cluster_id: int, run_id: int) -> ClusterOut:
    row = fetchone_mapping(
        con.execute(sa.select(clusters).where(clusters.c.cluster_id == cluster_id))
    )
    return _cluster_out(
        row,
        run_id,
        parent_labels=_parent_label_map(con, run_id),
        doc_count=_cluster_doc_count(con, cluster_id, run_id),
    )


def _apply_cluster_label(
    con,
    row: dict,
    run_id: int,
    *,
    override: str | None = None,
) -> tuple[str, int, int] | None:
    """Slugify the cluster label (or override) and apply it as an overlay tag.

    Returns ``(tag, applied, skipped)`` or ``None`` when no usable tag exists.
    """
    cid = row["cluster_id"]
    raw = (override if override else row["label"]) or ""
    tag = label_to_tag(raw.strip(), cid)
    if not tag:
        return None
    doc_ids = cluster_document_ids(con, cid, run_id)
    origin = _cluster_tag_origin(int(row.get("level") or 1))
    applied, skipped = apply_tag_to_documents(con, doc_ids, tag, origin=origin)
    return tag, applied, skipped


def _cluster_counts(con, run_id: int) -> dict[int, int]:
    rows = con.execute(
        sa.select(
            cluster_assignments.c.cluster_id,
            sa.func.count().label("n"),
        )
        .where(cluster_assignments.c.run_id == run_id)
        .group_by(cluster_assignments.c.cluster_id)
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def _cluster_out(
    row: dict,
    run_id: int,
    *,
    parent_labels: dict[int, str] | None = None,
    doc_counts: dict[int, int] | None = None,
    doc_count: int | None = None,
) -> ClusterOut:
    cid = row["cluster_id"]
    n = doc_count
    if n is None:
        n = (doc_counts or {}).get(cid, 0)
    level = int(row.get("level") or 1)
    parent_id = row.get("parent_cluster_id")
    parent_label = None
    if parent_id and parent_labels:
        parent_label = parent_labels.get(parent_id)
    return ClusterOut(
        cluster_id=cid,
        label=row["label"] or "",
        description=row["description"],
        run_id=run_id,
        doc_count=n,
        level=level,
        parent_cluster_id=parent_id,
        parent_label=parent_label,
    )


@router.get("", response_model=list[ClusterOut])
def list_clusters(engine=Depends(get_engine)):
    with engine.connect() as con:
        run_id = fetch_active_run_id(con)
        if not run_id:
            return []
        rows = fetchall_mappings(
            con.execute(sa.select(clusters).where(clusters.c.run_id == run_id))
        )
        parents = _parent_label_map(con, run_id)
        counts = _cluster_counts(con, run_id)
        out = [_cluster_out(r, run_id, parent_labels=parents, doc_counts=counts) for r in rows]
    return sorted(out, key=lambda x: (x.level, x.parent_cluster_id or 0, -x.doc_count))


@router.post("/apply-all-tags", response_model=ApplyAllTagsResult)
def apply_all_tags(engine=Depends(get_engine)):
    """Apply each cluster's stored label as an overlay tag on its documents."""
    results: list[ApplyTagResult] = []
    total_applied = total_skipped = 0

    with engine.begin() as con:
        run_id = fetch_active_run_id(con)
        if not run_id:
            raise HTTPException(404, "No active cluster run")

        rows = fetchall_mappings(
            con.execute(sa.select(clusters).where(clusters.c.run_id == run_id))
        )
        for row in rows:
            res = _apply_cluster_label(con, row, run_id)
            if not res:
                continue
            tag, applied, skipped = res
            results.append(
                ApplyTagResult(
                    cluster_id=row["cluster_id"],
                    tag=tag,
                    applied=applied,
                    skipped=skipped,
                )
            )
            total_applied += applied
            total_skipped += skipped

    return ApplyAllTagsResult(
        clusters=results,
        total_applied=total_applied,
        total_skipped=total_skipped,
    )


@router.get("/scatter/points", response_model=list[UmapPoint])
def scatter_points(engine=Depends(get_engine)):
    """Return persisted 2-D UMAP coordinates for the active run."""
    with engine.connect() as con:
        run_id = fetch_active_run_id(con)
        if not run_id:
            return []

        row = con.execute(
            sa.select(cluster_runs.c.umap_points).where(cluster_runs.c.run_id == run_id)
        ).fetchone()
        if not row or not row[0]:
            return []

        raw_points: list[dict] = json.loads(row[0])

        doc_to_db_cluster: dict[int, int] = {
            r[0]: r[1]
            for r in con.execute(
                sa.select(
                    cluster_assignments.c.document_id,
                    cluster_assignments.c.cluster_id,
                ).where(
                    (cluster_assignments.c.run_id == run_id) & (cluster_assignments.c.level == 1)
                )
            ).fetchall()
        }

        doc_titles: dict[int, str] = {}
        umap_doc_ids = [p["doc_id"] for p in raw_points]
        if umap_doc_ids:
            doc_titles = {
                r[0]: (r[1] or "")
                for r in con.execute(
                    sa.select(documents.c.id, documents.c.title).where(
                        documents.c.id.in_(umap_doc_ids)
                    )
                ).fetchall()
            }

    return [
        UmapPoint(
            doc_id=p["doc_id"],
            x=p["x"],
            y=p["y"],
            cluster_id=doc_to_db_cluster.get(p["doc_id"]),
            title=doc_titles.get(p["doc_id"], ""),
        )
        for p in raw_points
    ]


@router.get("/{cluster_id}", response_model=ClusterDetail)
def get_cluster(cluster_id: int, engine=Depends(get_engine)):
    with engine.connect() as con:
        run_id = fetch_active_run_id(con)
        row = fetchone_mapping(
            con.execute(sa.select(clusters).where(clusters.c.cluster_id == cluster_id))
        )
        if not row:
            raise HTTPException(404, "Cluster not found")
        if not run_id:
            raise HTTPException(404, "No active cluster run")
        top_tags = top_tags_for_cluster(con, cluster_id, run_id)
        parents = _parent_label_map(con, run_id)
        n = _cluster_doc_count(con, cluster_id, run_id)
        base = _cluster_out(row, run_id, parent_labels=parents, doc_count=n)
    return ClusterDetail(**base.model_dump(), top_tags=top_tags)


@router.patch("/{cluster_id}", response_model=ClusterOut)
def patch_cluster(
    cluster_id: int,
    req: ClusterPatchRequest,
    engine=Depends(get_engine),
):
    """Persist a manually edited cluster label (and optional description)."""
    label = req.label.strip()
    if not label:
        raise HTTPException(400, "Label cannot be empty")

    with engine.begin() as con:
        run_id = fetch_active_run_id(con)
        if not run_id:
            raise HTTPException(404, "No active cluster run")

        row = fetchone_mapping(
            con.execute(sa.select(clusters).where(clusters.c.cluster_id == cluster_id))
        )
        if not row or row["run_id"] != run_id:
            raise HTTPException(404, "Cluster not found")

        now = int(time.time())
        con.execute(
            clusters.update()
            .where(clusters.c.cluster_id == cluster_id)
            .values(
                label=label,
                description=req.description,
                created_at=now,
            )
        )
        return _cluster_out_by_id(con, cluster_id, run_id)


@router.post("/{cluster_id}/regenerate-label", response_model=ClusterOut)
def regenerate_cluster_label(cluster_id: int, engine=Depends(get_engine)):
    """Re-run LLM cluster labelling for one cluster."""
    with engine.connect() as con:
        run_id = fetch_active_run_id(con)
        if not run_id:
            raise HTTPException(404, "No active cluster run")
        row = fetchone_mapping(
            con.execute(sa.select(clusters).where(clusters.c.cluster_id == cluster_id))
        )
        if not row or row["run_id"] != run_id:
            raise HTTPException(404, "Cluster not found")

    try:
        relabel_single_cluster(cluster_id, run_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e

    with engine.connect() as con:
        return _cluster_out_by_id(con, cluster_id, run_id)


@router.post("/{cluster_id}/apply-tag", response_model=ApplyTagResult)
def apply_cluster_tag(
    cluster_id: int,
    req: ApplyTagRequest | None = None,
    engine=Depends(get_engine),
):
    """Apply the cluster label (slugified) or an override tag to every document."""
    with engine.begin() as con:
        run_id = fetch_active_run_id(con)
        if not run_id:
            raise HTTPException(404, "No active cluster run")

        row = fetchone_mapping(
            con.execute(sa.select(clusters).where(clusters.c.cluster_id == cluster_id))
        )
        if not row or row["run_id"] != run_id:
            raise HTTPException(404, "Cluster not found")

        res = _apply_cluster_label(
            con,
            row,
            run_id,
            override=(req.tag if req and req.tag else None),
        )
        if not res:
            raise HTTPException(400, "No tag available — set a cluster label first")
        tag, applied, skipped = res

    return ApplyTagResult(
        cluster_id=cluster_id,
        tag=tag,
        applied=applied,
        skipped=skipped,
    )


@router.get("/{cluster_id}/documents")
def cluster_documents(
    cluster_id: int,
    limit: int = 20,
    offset: int = 0,
    engine=Depends(get_engine),
):
    with engine.connect() as con:
        run_id = fetch_active_run_id(con)
        rows = fetchall_mappings(
            con.execute(
                sa.select(documents)
                .join(cluster_assignments, cluster_assignments.c.document_id == documents.c.id)
                .where(
                    (cluster_assignments.c.cluster_id == cluster_id)
                    & (cluster_assignments.c.run_id == run_id)
                )
                .limit(limit)
                .offset(offset)
            )
        )
    return [
        {"id": r["id"], "title": r["title"], "source": r["source"], "date_added": r["date_added"]}
        for r in rows
    ]
