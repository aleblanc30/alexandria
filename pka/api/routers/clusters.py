"""``/clusters`` — list, detail, document membership, and 2-D scatter layout."""
import json

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException

from pka.api.db_rows import fetchall_mappings, fetchone_mapping
from pka.api.dependencies import get_engine
from pka.api.schemas.clusters import (
    ApplyAllTagsResult,
    ApplyTagRequest,
    ApplyTagResult,
    ClusterDetail,
    ClusterOut,
    TagCandidateOut,
    UmapPoint,
)
from pka.constants import TagOrigin
from pka.clustering.tag_suggestions import (
    TagCandidate,
    apply_tag_to_documents,
    build_tag_suggestions,
    cluster_document_ids,
    top_tags_for_cluster,
)
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
        sa.select(clusters.c.cluster_id, clusters.c.label)
        .where(clusters.c.run_id == run_id)
    ).fetchall()
    return {r[0]: r[1] or "" for r in rows}


def _active_run(con) -> int | None:
    row = con.execute(
        sa.select(cluster_runs.c.run_id)
        .where(cluster_runs.c.accepted == True)  # noqa: E712
        .order_by(cluster_runs.c.run_id.desc()).limit(1)
    ).fetchone()
    return row[0] if row else None


def _candidate_out(c: TagCandidate) -> TagCandidateOut:
    return TagCandidateOut(
        tag=c.tag, source=c.source, coverage=c.coverage, doc_count=c.doc_count,
    )


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
    con,
    row: dict,
    run_id: int,
    *,
    refresh: bool = False,
    parent_labels: dict[int, str] | None = None,
    doc_counts: dict[int, int] | None = None,
    use_llm: bool = True,
) -> ClusterOut:
    n = (doc_counts or {}).get(row["cluster_id"])
    if n is None:
        n = con.execute(
            sa.select(sa.func.count()).select_from(cluster_assignments)
            .where((cluster_assignments.c.cluster_id == row["cluster_id"]) &
                   (cluster_assignments.c.run_id == run_id))
        ).scalar() or 0
    result = build_tag_suggestions(
        con, row["cluster_id"], run_id, row["label"],
        refresh=refresh, use_llm=use_llm,
    )
    level = int(row.get("level") or 1)
    parent_id = row.get("parent_cluster_id")
    parent_label = None
    if parent_id and parent_labels:
        parent_label = parent_labels.get(parent_id)
    return ClusterOut(
        cluster_id=row["cluster_id"],
        label=row["label"] or "",
        description=row["description"],
        run_id=run_id,
        doc_count=n,
        level=level,
        parent_cluster_id=parent_id,
        parent_label=parent_label,
        suggested_tag=result.suggested_tag,
        tag_candidates=[_candidate_out(c) for c in result.candidates],
        llm_error=result.llm_error,
    )


@router.get("", response_model=list[ClusterOut])
async def list_clusters(engine=Depends(get_engine)):
    with engine.connect() as con:
        run_id = _active_run(con)
        if not run_id:
            return []
        rows = fetchall_mappings(con.execute(
            sa.select(clusters).where(clusters.c.run_id == run_id)
        ))
        parents = _parent_label_map(con, run_id)
        counts = _cluster_counts(con, run_id)
        out = [
            _cluster_out(
                con, r, run_id,
                parent_labels=parents,
                doc_counts=counts,
                use_llm=False,
            )
            for r in rows
        ]
    return sorted(out, key=lambda x: (x.level, x.parent_cluster_id or 0, -x.doc_count))


@router.post("/apply-all-tags", response_model=ApplyAllTagsResult)
async def apply_all_tags(engine=Depends(get_engine)):
    """Apply each cluster's suggested tag to all documents in that cluster."""
    results: list[ApplyTagResult] = []
    total_applied = total_skipped = 0

    with engine.begin() as con:
        run_id = _active_run(con)
        if not run_id:
            raise HTTPException(404, "No active cluster run")

        rows = fetchall_mappings(con.execute(
            sa.select(clusters).where(clusters.c.run_id == run_id)
        ))
        for row in rows:
            cid = row["cluster_id"]
            suggestion = build_tag_suggestions(
                con, cid, run_id, row["label"], use_llm=False,
            )
            tag = suggestion.suggested_tag
            if not tag:
                continue
            doc_ids = cluster_document_ids(con, cid, run_id)
            origin = _cluster_tag_origin(int(row.get("level") or 1))
            applied, skipped = apply_tag_to_documents(con, doc_ids, tag, origin=origin)
            results.append(ApplyTagResult(
                cluster_id=cid, tag=tag, applied=applied, skipped=skipped,
            ))
            total_applied += applied
            total_skipped += skipped

    return ApplyAllTagsResult(
        clusters=results,
        total_applied=total_applied,
        total_skipped=total_skipped,
    )


@router.get("/scatter/points", response_model=list[UmapPoint])
async def scatter_points(engine=Depends(get_engine)):
    """Return persisted 2-D UMAP coordinates for the active run."""
    with engine.connect() as con:
        run_id = _active_run(con)
        if not run_id:
            return []

        row = con.execute(
            sa.select(cluster_runs.c.umap_points)
            .where(cluster_runs.c.run_id == run_id)
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
                    (cluster_assignments.c.run_id == run_id)
                    & (cluster_assignments.c.level == 1)
                )
            ).fetchall()
        }

        doc_titles: dict[int, str] = {}
        umap_doc_ids = [p["doc_id"] for p in raw_points]
        if umap_doc_ids:
            doc_titles = {
                r[0]: (r[1] or "")
                for r in con.execute(
                    sa.select(documents.c.id, documents.c.title)
                    .where(documents.c.id.in_(umap_doc_ids))
                ).fetchall()
            }

    return [
        UmapPoint(
            doc_id     = p["doc_id"],
            x          = p["x"],
            y          = p["y"],
            cluster_id = doc_to_db_cluster.get(p["doc_id"]),
            title      = doc_titles.get(p["doc_id"], ""),
        )
        for p in raw_points
    ]


@router.get("/{cluster_id}", response_model=ClusterDetail)
async def get_cluster(cluster_id: int, engine=Depends(get_engine)):
    with engine.connect() as con:
        run_id = _active_run(con)
        row = fetchone_mapping(con.execute(
            sa.select(clusters).where(clusters.c.cluster_id == cluster_id)
        ))
        if not row:
            raise HTTPException(404, "Cluster not found")
        if not run_id:
            raise HTTPException(404, "No active cluster run")
        top_tags = top_tags_for_cluster(con, cluster_id, run_id)
        parents = _parent_label_map(con, run_id)
        base = _cluster_out(con, row, run_id, parent_labels=parents)
    return ClusterDetail(**base.model_dump(), top_tags=top_tags)


@router.post("/{cluster_id}/regenerate-tag", response_model=ClusterOut)
async def regenerate_cluster_tag(cluster_id: int, engine=Depends(get_engine)):
    """Re-run the LLM tag suggestion for one cluster."""
    with engine.connect() as con:
        run_id = _active_run(con)
        if not run_id:
            raise HTTPException(404, "No active cluster run")
        row = fetchone_mapping(con.execute(
            sa.select(clusters).where(clusters.c.cluster_id == cluster_id)
        ))
        if not row or row["run_id"] != run_id:
            raise HTTPException(404, "Cluster not found")
        parents = _parent_label_map(con, run_id)
        return _cluster_out(con, row, run_id, refresh=True, parent_labels=parents)


@router.post("/{cluster_id}/apply-tag", response_model=ApplyTagResult)
async def apply_cluster_tag(
    cluster_id: int,
    req: ApplyTagRequest | None = None,
    engine=Depends(get_engine),
):
    """Apply the suggested tag (or an override) to every document in the cluster."""
    with engine.begin() as con:
        run_id = _active_run(con)
        if not run_id:
            raise HTTPException(404, "No active cluster run")

        row = fetchone_mapping(con.execute(
            sa.select(clusters).where(clusters.c.cluster_id == cluster_id)
        ))
        if not row or row["run_id"] != run_id:
            raise HTTPException(404, "Cluster not found")

        suggestion = build_tag_suggestions(con, cluster_id, run_id, row["label"], use_llm=False)
        default_tag = suggestion.suggested_tag
        tag = (req.tag.strip() if req and req.tag else default_tag).strip()
        if not tag:
            detail = suggestion.llm_error or "No tag suggestion available"
            raise HTTPException(400, detail)

        doc_ids = cluster_document_ids(con, cluster_id, run_id)
        origin = _cluster_tag_origin(int(row.get("level") or 1))
        applied, skipped = apply_tag_to_documents(con, doc_ids, tag, origin=origin)

    return ApplyTagResult(
        cluster_id=cluster_id, tag=tag, applied=applied, skipped=skipped,
    )


@router.get("/{cluster_id}/documents")
async def cluster_documents(
    cluster_id: int,
    limit: int = 20,
    offset: int = 0,
    engine=Depends(get_engine),
):
    with engine.connect() as con:
        run_id = _active_run(con)
        rows = fetchall_mappings(con.execute(
            sa.select(documents)
            .join(cluster_assignments,
                  cluster_assignments.c.document_id == documents.c.id)
            .where((cluster_assignments.c.cluster_id == cluster_id) &
                   (cluster_assignments.c.run_id == run_id))
            .limit(limit).offset(offset)
        ))
    return [{"id": r["id"], "title": r["title"], "source": r["source"],
             "date_added": r["date_added"]} for r in rows]
