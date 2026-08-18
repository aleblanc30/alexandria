"""Shared serialization of document rows into API response models.

Both the search results path (batched) and the single-document detail path build
the same set of relations (source tags, overlay tags, cluster assignment,
description), so they live here to avoid drift and N+1 duplication.
"""
import sqlalchemy as sa

from pka.api.db_rows import fetchall_mappings, fetchone_mapping
from pka.api.schemas.documents import DocumentDetail, DocumentOut, EnrichmentOut, ImageDetail
from pka.constants import Source
from pka.db.queries import (
    _batch_first_chunk_map,
    document_description,
    document_enrichment,
    resolve_description,
)
from pka.db.schema import (
    chunks,
    cluster_assignments,
    clusters,
    documents,
    images,
    overlay_tags,
    source_collections,
    source_tags,
)


def documents_out_batch(
    doc_ids_with_sim: list[tuple[int, float | None]],
    con,
    run_id: int | None,
) -> list[DocumentOut]:
    """Build :class:`DocumentOut` list with a single batched query per relation."""
    if not doc_ids_with_sim:
        return []

    sim_map = dict(doc_ids_with_sim)
    doc_ids = list(sim_map.keys())

    doc_rows = {
        r["id"]: r for r in fetchall_mappings(con.execute(
            sa.select(documents).where(documents.c.id.in_(doc_ids))
        ))
    }

    source_tag_map: dict[int, list[str]] = {did: [] for did in doc_ids}
    for r in con.execute(
        sa.select(source_tags.c.document_id, source_tags.c.tag_string)
        .where(source_tags.c.document_id.in_(doc_ids))
    ).fetchall():
        source_tag_map[r[0]].append(r[1])

    overlay_tag_map: dict[int, list[dict]] = {did: [] for did in doc_ids}
    for r in con.execute(
        sa.select(
            overlay_tags.c.document_id,
            overlay_tags.c.tag,
            overlay_tags.c.origin,
            overlay_tags.c.confidence,
        ).where(overlay_tags.c.document_id.in_(doc_ids))
    ).fetchall():
        overlay_tag_map[r[0]].append(
            {"tag": r[1], "origin": r[2], "confidence": r[3]}
        )

    cluster_map: dict[int, tuple[int, str]] = {}
    if run_id:
        for r in con.execute(
            sa.select(
                cluster_assignments.c.document_id,
                cluster_assignments.c.cluster_id,
                clusters.c.label,
            )
            .join(clusters, clusters.c.cluster_id == cluster_assignments.c.cluster_id)
            .where(
                (cluster_assignments.c.run_id == run_id) &
                (cluster_assignments.c.document_id.in_(doc_ids)) &
                (cluster_assignments.c.level == 1)
            )
        ).fetchall():
            cluster_map[r[0]] = (r[1], r[2])

    needs_chunk = [
        did for did in doc_ids
        if did in doc_rows
        and not (doc_rows[did].get("card_summary") and str(doc_rows[did]["card_summary"]).strip())
    ]
    chunk_map = _batch_first_chunk_map(con, needs_chunk)

    out: list[DocumentOut] = []
    for doc_id, sim in doc_ids_with_sim:
        row = doc_rows.get(doc_id)
        if not row:
            continue
        cid, clabel = cluster_map.get(doc_id, (None, None))
        description = resolve_description(row.get("card_summary"), chunk_map.get(doc_id))
        out.append(DocumentOut(
            id=doc_id, source=row["source"], source_id=row["source_id"],
            title=row["title"] or "", url_or_path=row["url_or_path"],
            archive_url=row.get("archive_url"),
            zotero_attachment_key=row.get("zotero_attachment_key"),
            date_added=row["date_added"], fetch_status=row["fetch_status"],
            source_tags=source_tag_map.get(doc_id, []),
            overlay_tags=overlay_tag_map.get(doc_id, []),
            cluster_id=cid, cluster_label=clabel,
            similarity=sim,
            description=description,
            note=row.get("note"),
        ))
    return out


# Human-readable name for each rung of the enrichment ladder (DESIGN.md §3.2).
# Owned by the backend so the ladder stays the single source of truth.
_ENRICHMENT_LABELS = {
    "isbn":         "Open Library · ISBN",
    "search":       "Open Library · title match",
    "google_books": "Google Books",
    "brave":        "Brave search",
    "local_model":  "Local model",
}
_ENRICHMENT_FALLBACK_LABEL = "External source"


def enrichment_out(rows: list[dict]) -> list[EnrichmentOut]:
    """Turn :func:`pka.db.queries.document_enrichment` rows into API models.

    A ``summary`` chunk stores no ``resolved_by`` — it is normalised to
    ``local_model`` so the frontend gets one uniform shape for every rung.
    """
    out: list[EnrichmentOut] = []
    for row in rows:
        kind = row["chunk_pass"]
        resolved_by = row["resolved_by"] or ("local_model" if kind == "summary" else None)
        out.append(EnrichmentOut(
            kind        = kind,
            resolved_by = resolved_by,
            label       = _ENRICHMENT_LABELS.get(resolved_by, _ENRICHMENT_FALLBACK_LABEL),
            source_ref  = row["source_ref"],
            ref_title   = row["ref_title"],
            text        = row["text"] or "",
        ))
    return out


def document_detail(con, doc_id: int, run_id: int | None) -> DocumentDetail | None:
    """Build a single :class:`DocumentDetail`, or ``None`` if the document is missing."""
    row = fetchone_mapping(con.execute(
        sa.select(documents).where(documents.c.id == doc_id)
    ))
    if not row:
        return None

    stags = [r[0] for r in con.execute(
        sa.select(source_tags.c.tag_string)
        .where(source_tags.c.document_id == doc_id)
    ).fetchall()]
    otags = [{"tag": r[0], "origin": r[1], "confidence": r[2]}
             for r in con.execute(
        sa.select(overlay_tags.c.tag, overlay_tags.c.origin, overlay_tags.c.confidence)
        .where(overlay_tags.c.document_id == doc_id)
    ).fetchall()]
    colls = [r[0] for r in con.execute(
        sa.select(source_collections.c.collection)
        .where(source_collections.c.document_id == doc_id)
    ).fetchall()]
    n_chunks = con.execute(
        sa.select(sa.func.count()).select_from(chunks)
        .where(chunks.c.document_id == doc_id)
    ).scalar() or 0
    description = document_description(con, doc_id)

    image_detail = None
    if row["source"] == Source.IMAGE:
        img_row = fetchone_mapping(con.execute(
            sa.select(images.c.image_type, images.c.ocr_text)
            .where(images.c.document_id == doc_id)
        ))
        if img_row:
            image_detail = ImageDetail(
                image_type=img_row["image_type"],
                ocr_text=img_row["ocr_text"],
            )

    cluster_id = cluster_label = None
    if run_id:
        ca = con.execute(
            sa.select(cluster_assignments.c.cluster_id)
            .where(
                (cluster_assignments.c.document_id == doc_id)
                & (cluster_assignments.c.run_id == run_id)
                & (cluster_assignments.c.level == 1)
            )
        ).fetchone()
        if ca:
            cluster_id = ca[0]
            cl = con.execute(
                sa.select(clusters.c.label)
                .where(clusters.c.cluster_id == cluster_id)
            ).fetchone()
            cluster_label = cl[0] if cl else None

    return DocumentDetail(
        id=doc_id, source=row["source"], source_id=row["source_id"],
        title=row["title"] or "", url_or_path=row["url_or_path"],
        archive_url=row.get("archive_url"),
        zotero_attachment_key=row.get("zotero_attachment_key"),
        date_added=row["date_added"], fetch_status=row["fetch_status"],
        source_tags=stags, overlay_tags=otags,
        cluster_id=cluster_id, cluster_label=cluster_label,
        description=description,
        note=row.get("note"),
        collections=colls, chunks_count=n_chunks,
        image=image_detail,
        enrichment=enrichment_out(document_enrichment([doc_id]).get(doc_id, [])),
    )
