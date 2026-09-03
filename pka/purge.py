"""Selective purge of individual archive artifacts.

Generalises :mod:`pka.cli.purge_source` (all-or-nothing, per source) and
:mod:`pka.cli.purge_cluster_runs` (which already implements this pattern for
clustering) into one registry, per ``planning/PURGE_AND_PROVENANCE_PLAN.md``
§5.2. Each :class:`PurgeTarget` names an artifact, how to count it (the dry
run) and how to delete it; an optional ``source`` scopes both to one connector,
so "purge Firefox summaries" and "purge all summaries" are the same code path.

**Invariant:** every key a target's ``count`` reports is also reported by its
``purge``, with the same value — a dry run is a promise about what pressing the
button will do. ``purge`` may add keys of its own (``vectors_purged``).

There is deliberately no per-target "retrigger" callable. For most targets,
clearing the artifact a pipeline's skip-gate checks *is* the retrigger — the
next sync regenerates it — so :attr:`PurgeTarget.retrigger` only names the
endpoint or command that does that. The exception is ``summaries``, whose skip
gate is keyed on "has any chunk at all" rather than on the summary; see the
plan's §5.2.1 and the ``enrich`` pass it calls for.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

import sqlalchemy as sa

from pka.constants import EnrichmentKind, FetchStatus, Source, TagOrigin
from pka.db.queries import get_engine
from pka.db.schema import chunks, documents, enrichment_runs, fetch_log, images, overlay_tags
from pka.storage import vector_store

# SQLite binds one variable per id in an ``IN (...)`` list and
# SQLITE_MAX_VARIABLE_NUMBER is 32766, so every id list here goes through
# _batches. Same ceiling as pka/cli/purge_source.py, which keeps its own copy.
_ID_BATCH_SIZE = 5_000


def _batches(ids: list) -> Iterator[list]:
    for i in range(0, len(ids), _ID_BATCH_SIZE):
        yield ids[i : i + _ID_BATCH_SIZE]


# overlay_tags origins written by inference (clustering, LLM tagging, vision
# classification) rather than by the user. The complement — manual, learned,
# source — is Tier 1/3 and is never touched here (plan §3).
_MACHINE_TAG_ORIGINS = (
    str(TagOrigin.LLM),
    str(TagOrigin.CLUSTER_L1),
    str(TagOrigin.CLUSTER_L2),
    str(TagOrigin.INFERRED),
)

# Passes that are *not* fetched body text; excluding them leaves the summary,
# metadata and synopsis chunks alone. A body chunk's ``chunk_pass`` is NULL for
# every source but Calibre (which tags it "fulltext"), so NULL counts as body.
_NON_FETCHED_PASSES = ("summary", "external_synopsis", "metadata")


def body_chunk_predicate():
    """SQL predicate selecting fetched-body chunks, not enrichment passes.

    Shared with :mod:`pka.ingestion.enrich`, which reassembles exactly the
    chunks this deletes — the two must not drift apart.
    """
    return sa.or_(
        chunks.c.chunk_pass.is_(None),
        chunks.c.chunk_pass.notin_(_NON_FETCHED_PASSES),
    )


@dataclass(frozen=True)
class PurgeScope:
    """What a purge is narrowed to: a source, and optionally a provenance.

    The provenance fields are what stamping buys (plan §6.3): "purge the
    summaries the old model made, keep the ones I am still using" is one of
    these, and was unexpressible before ``enrichment_runs`` existed.
    ``unknown`` selects the pre-provenance backlog — artifacts made before
    stamping shipped, whose ``*_run_id`` is honestly NULL.
    """

    source: str | None = None
    run_id: int | None = None
    provider: str | None = None
    model: str | None = None
    unknown: bool = False

    @property
    def has_provenance(self) -> bool:
        return self.unknown or any((self.run_id is not None, self.provider, self.model))


@dataclass(frozen=True)
class PurgeTarget:
    key: str
    label: str
    # Data tier per the plan's §3 taxonomy: 2 = model-derived (re-runnable at a
    # cost in inference), 3 = source-derived (re-runnable by the connector).
    # Tier 1 (user-authored) is deliberately absent — nothing here may touch it.
    tier: int
    count: Callable[[PurgeScope], dict[str, int]]
    purge: Callable[[PurgeScope], dict[str, int]]
    retrigger: str | None
    # Whether this target's artifact carries a run stamp. A target that cannot
    # honour a provenance filter must refuse it rather than ignore it: silently
    # widening "purge what the old model made" to "purge everything" is the one
    # failure this whole feature exists to prevent.
    provenance: bool = False


def _source_clause(query, source: str | None):
    return query if source is None else query.where(documents.c.source == str(source))


def _doc_ids(con, source: str | None) -> list[int]:
    return [r[0] for r in con.execute(_source_clause(sa.select(documents.c.id), source)).fetchall()]


def _vector_ids_for_chunks(con, chunk_ids: list[int]) -> list[str]:
    out: list[str] = []
    for batch in _batches(chunk_ids):
        out.extend(
            r[0]
            for r in con.execute(
                sa.select(chunks.c.vector_id)
                .where(chunks.c.id.in_(batch))
                .where(chunks.c.vector_id.isnot(None))
            ).fetchall()
        )
    return out


def _delete_chunks(con, chunk_ids: list[int]) -> None:
    for batch in _batches(chunk_ids):
        con.execute(chunks.delete().where(chunks.c.id.in_(batch)))


# ── summaries ────────────────────────────────────────────────────────────────


def _summary_provenance_clause(scope: PurgeScope):
    """Narrow to summaries made by a particular run / backend, or by none."""
    if scope.unknown:
        return documents.c.summary_run_id.is_(None)
    if scope.run_id is not None:
        return documents.c.summary_run_id == scope.run_id
    if scope.provider or scope.model:
        runs = sa.select(enrichment_runs.c.run_id).where(
            enrichment_runs.c.kind == str(EnrichmentKind.SUMMARY)
        )
        if scope.provider:
            runs = runs.where(enrichment_runs.c.provider == scope.provider)
        if scope.model:
            runs = runs.where(enrichment_runs.c.model == scope.model)
        return documents.c.summary_run_id.in_(runs)
    return None


def _scoped_summaries(query, scope: PurgeScope):
    query = _source_clause(query, scope.source)
    clause = _summary_provenance_clause(scope)
    return query if clause is None else query.where(clause)


def _summary_chunk_ids(con, scope: PurgeScope) -> list[int]:
    # Joined to documents so a provenance filter — which lives on the document
    # row — narrows the chunks it deletes as well as the cached text it clears.
    q = sa.select(chunks.c.id).select_from(
        chunks.join(documents, chunks.c.document_id == documents.c.id)
    )
    q = _scoped_summaries(q.where(chunks.c.chunk_pass == "summary"), scope)
    return [r[0] for r in con.execute(q).fetchall()]


def _summarised_doc_count(con, scope: PurgeScope) -> int:
    q = (
        sa.select(sa.func.count())
        .select_from(documents)
        .where(documents.c.generated_summary.isnot(None))
    )
    return con.execute(_scoped_summaries(q, scope)).scalar() or 0


def _count_summaries(scope: PurgeScope) -> dict[str, int]:
    with get_engine().connect() as con:
        return {
            "documents": _summarised_doc_count(con, scope),
            "chunks": len(_summary_chunk_ids(con, scope)),
        }


def _purge_summaries(scope: PurgeScope) -> dict[str, int]:
    eng = get_engine()
    with eng.connect() as con:
        chunk_ids = _summary_chunk_ids(con, scope)
        vector_ids = _vector_ids_for_chunks(con, chunk_ids)

    counts = {"documents": 0, "chunks": len(chunk_ids), "vectors_purged": 0}
    if vector_ids:
        counts["vectors_purged"] = vector_store.purge_vectors(vector_ids)

    with eng.begin() as con:
        _delete_chunks(con, chunk_ids)
        upd = (
            documents.update()
            .where(documents.c.generated_summary.isnot(None))
            # The stamp goes with the text: a run id pointing at a summary that
            # is no longer there would make the next count promise a deletion
            # it cannot make.
            .values(generated_summary=None, summary_run_id=None)
        )
        counts["documents"] = con.execute(_scoped_summaries(upd, scope)).rowcount
    return counts


# ── image_text (description / OCR / book extraction) ────────────────────────
# Scoped to the image source by construction, so a source filter naming anything
# else selects nothing rather than silently ignoring the filter.


def _wrong_source(source: str | None) -> bool:
    return source is not None and str(source) != str(Source.IMAGE)


def _indexed_image_doc_ids(con) -> list[int]:
    return [
        r[0]
        for r in con.execute(
            sa.select(images.c.document_id).where(images.c.indexed_at.isnot(None))
        ).fetchall()
    ]


def _chunk_ids_for_docs(con, doc_ids: list[int]) -> list[int]:
    return [
        r[0]
        for batch in _batches(doc_ids)
        for r in con.execute(
            sa.select(chunks.c.id).where(chunks.c.document_id.in_(batch))
        ).fetchall()
    ]


def _count_image_text(scope: PurgeScope) -> dict[str, int]:
    source = scope.source
    if _wrong_source(source):
        return {"images": 0, "chunks": 0}
    with get_engine().connect() as con:
        doc_ids = _indexed_image_doc_ids(con)
        return {"images": len(doc_ids), "chunks": len(_chunk_ids_for_docs(con, doc_ids))}


def _purge_image_text(scope: PurgeScope) -> dict[str, int]:
    """Clear the VLM/OCR output and re-open the skip gate.

    ``indexed_at`` is what :func:`image_pipeline._image_already_embedded` gates
    on, so nulling it is the whole retrigger — the next image sync re-describes
    the file, which is still on disk. The document's chunks go too: the pipeline
    appends at ``existing_chunk_count`` without deduplicating, so leaving them
    would double every image's searchable text on the next run.
    """
    source = scope.source
    if _wrong_source(source):
        return {"images": 0, "chunks": 0}
    eng = get_engine()
    with eng.connect() as con:
        doc_ids = _indexed_image_doc_ids(con)
        chunk_ids = _chunk_ids_for_docs(con, doc_ids)
        vector_ids = _vector_ids_for_chunks(con, chunk_ids)

    counts = {"images": len(doc_ids), "chunks": len(chunk_ids), "vectors_purged": 0}
    if vector_ids:
        counts["vectors_purged"] = vector_store.purge_vectors(vector_ids)

    with eng.begin() as con:
        _delete_chunks(con, chunk_ids)
        for batch in _batches(doc_ids):
            con.execute(
                images.update()
                .where(images.c.document_id.in_(batch))
                .values(description=None, ocr_text=None, books_json=None, indexed_at=None)
            )
    return counts


# ── clip_vectors ─────────────────────────────────────────────────────────────


def _clip_vector_ids(con) -> list[str]:
    return [
        r[0]
        for r in con.execute(
            sa.select(images.c.clip_vector_id).where(images.c.clip_vector_id.isnot(None))
        ).fetchall()
    ]


def _count_clip_vectors(scope: PurgeScope) -> dict[str, int]:
    source = scope.source
    if _wrong_source(source):
        return {"images": 0}
    with get_engine().connect() as con:
        return {"images": len(_clip_vector_ids(con))}


def _purge_clip_vectors(scope: PurgeScope) -> dict[str, int]:
    source = scope.source
    if _wrong_source(source):
        return {"images": 0, "clip_vectors_purged": 0}
    from pka.ingestion.image_pipeline import delete_clip_vectors

    eng = get_engine()
    with eng.connect() as con:
        vector_ids = _clip_vector_ids(con)

    counts = {"images": len(vector_ids), "clip_vectors_purged": 0}
    if vector_ids:
        counts["clip_vectors_purged"] = delete_clip_vectors(vector_ids)

    with eng.begin() as con:
        con.execute(
            images.update().where(images.c.clip_vector_id.isnot(None)).values(clip_vector_id=None)
        )
    return counts


# ── machine_tags ─────────────────────────────────────────────────────────────


def _count_machine_tags(scope: PurgeScope) -> dict[str, int]:
    source = scope.source
    with get_engine().connect() as con:
        if source is None:
            n = (
                con.execute(
                    sa.select(sa.func.count())
                    .select_from(overlay_tags)
                    .where(overlay_tags.c.origin.in_(_MACHINE_TAG_ORIGINS))
                ).scalar()
                or 0
            )
        else:
            doc_ids = _doc_ids(con, source)
            n = sum(
                con.execute(
                    sa.select(sa.func.count())
                    .select_from(overlay_tags)
                    .where(overlay_tags.c.origin.in_(_MACHINE_TAG_ORIGINS))
                    .where(overlay_tags.c.document_id.in_(batch))
                ).scalar()
                or 0
                for batch in _batches(doc_ids)
            )
    return {"overlay_tags": n}


def _purge_machine_tags(scope: PurgeScope) -> dict[str, int]:
    source = scope.source
    eng = get_engine()
    machine = overlay_tags.c.origin.in_(_MACHINE_TAG_ORIGINS)
    if source is None:
        with eng.begin() as con:
            return {"overlay_tags": con.execute(overlay_tags.delete().where(machine)).rowcount}

    with eng.connect() as con:
        doc_ids = _doc_ids(con, source)
    with eng.begin() as con:
        n = sum(
            con.execute(
                overlay_tags.delete().where(machine).where(overlay_tags.c.document_id.in_(batch))
            ).rowcount
            for batch in _batches(doc_ids)
        )
    return {"overlay_tags": n}


# ── fetched_text ─────────────────────────────────────────────────────────────


def _fetched_text_rows(con, source: str | None) -> list:
    q = (
        sa.select(chunks.c.id, chunks.c.document_id)
        .select_from(chunks.join(documents, chunks.c.document_id == documents.c.id))
        # Image "body" text is the VLM/OCR output, which image_text owns.
        .where(documents.c.source != str(Source.IMAGE))
        .where(body_chunk_predicate())
    )
    return list(con.execute(_source_clause(q, source)).fetchall())


def _count_fetched_text(scope: PurgeScope) -> dict[str, int]:
    source = scope.source
    with get_engine().connect() as con:
        rows = _fetched_text_rows(con, source)
    return {"chunks": len(rows), "documents": len({r.document_id for r in rows})}


def _purge_fetched_text(scope: PurgeScope) -> dict[str, int]:
    """Drop body chunks and re-queue the documents that were fetched over HTTP.

    Only ``fetched`` documents go back to ``pending``: a Calibre book or Zotero
    PDF rests at ``available``/``missing`` and is re-read from disk, so moving
    it to ``pending`` would misdescribe it to the fetch dispatcher.
    """
    source = scope.source
    eng = get_engine()
    with eng.connect() as con:
        rows = _fetched_text_rows(con, source)
        chunk_ids = [r.id for r in rows]
        doc_ids = sorted({r.document_id for r in rows})
        vector_ids = _vector_ids_for_chunks(con, chunk_ids)

    counts = {"documents": len(doc_ids), "chunks": len(chunk_ids), "vectors_purged": 0}
    if vector_ids:
        counts["vectors_purged"] = vector_store.purge_vectors(vector_ids)

    with eng.begin() as con:
        _delete_chunks(con, chunk_ids)
        for batch in _batches(doc_ids):
            con.execute(
                documents.update()
                .where(documents.c.id.in_(batch))
                .where(documents.c.fetch_status == str(FetchStatus.FETCHED))
                .values(fetch_status=str(FetchStatus.PENDING))
            )
    return counts


# ── fetch_failures ───────────────────────────────────────────────────────────


def _unfetchable_doc_ids(con, source: str | None) -> list[int]:
    q = sa.select(documents.c.id).where(documents.c.fetch_status == str(FetchStatus.UNFETCHABLE))
    return [r[0] for r in con.execute(_source_clause(q, source)).fetchall()]


def _count_fetch_failures(scope: PurgeScope) -> dict[str, int]:
    source = scope.source
    with get_engine().connect() as con:
        doc_ids = _unfetchable_doc_ids(con, source)
        n_log = sum(
            con.execute(
                sa.select(sa.func.count())
                .select_from(fetch_log)
                .where(fetch_log.c.document_id.in_(batch))
            ).scalar()
            or 0
            for batch in _batches(doc_ids)
        )
    return {"documents": len(doc_ids), "fetch_log": n_log}


def _purge_fetch_failures(scope: PurgeScope) -> dict[str, int]:
    source = scope.source
    eng = get_engine()
    with eng.connect() as con:
        doc_ids = _unfetchable_doc_ids(con, source)

    counts = {"documents": 0, "fetch_log": 0}
    with eng.begin() as con:
        for batch in _batches(doc_ids):
            counts["fetch_log"] += con.execute(
                fetch_log.delete().where(fetch_log.c.document_id.in_(batch))
            ).rowcount
            counts["documents"] += con.execute(
                documents.update()
                .where(documents.c.id.in_(batch))
                .values(fetch_status=str(FetchStatus.PENDING))
            ).rowcount
    return counts


# ── vectors (chunk embeddings + doc_embedding) ──────────────────────────────


def _vectored_chunk_ids(con, source: str | None) -> list[int]:
    q = sa.select(chunks.c.id).select_from(
        chunks.join(documents, chunks.c.document_id == documents.c.id)
    )
    return [
        r[0]
        for r in con.execute(
            _source_clause(q.where(chunks.c.vector_id.isnot(None)), source)
        ).fetchall()
    ]


def _embedded_doc_count(con, source: str | None) -> int:
    q = (
        sa.select(sa.func.count())
        .select_from(documents)
        .where(documents.c.doc_embedding.isnot(None))
    )
    return con.execute(_source_clause(q, source)).scalar() or 0


def _count_vectors(scope: PurgeScope) -> dict[str, int]:
    source = scope.source
    with get_engine().connect() as con:
        return {
            "chunks": len(_vectored_chunk_ids(con, source)),
            "documents": _embedded_doc_count(con, source),
        }


def _purge_vectors(scope: PurgeScope) -> dict[str, int]:
    """Drop the chunk collection and forget every vector id.

    Chroma collections are dimension-locked and have no per-id clear that leaves
    the rest searchable, so an archive-wide drop is the only sound option even
    for a source-scoped purge (plan §10). Nothing is lost: ``rebuild_from_chunks``
    — the retrigger — re-embeds everything from ``chunks.text``, which stays.
    """
    source = scope.source
    eng = get_engine()
    with eng.connect() as con:
        chunk_ids = _vectored_chunk_ids(con, source)
        doc_count = _embedded_doc_count(con, source)

    vector_store.drop_document_collection()

    with eng.begin() as con:
        for batch in _batches(chunk_ids):
            con.execute(chunks.update().where(chunks.c.id.in_(batch)).values(vector_id=None))
        upd = (
            documents.update()
            .where(documents.c.doc_embedding.isnot(None))
            .values(doc_embedding=None)
        )
        con.execute(_source_clause(upd, source))
    return {"chunks": len(chunk_ids), "documents": doc_count}


# ── cluster_runs (delegates to the existing module) ─────────────────────────
# force stays False in both directions: the accepted run is skipped, and a dry
# run that counted it would promise a deletion the purge then refuses to make.


def _count_cluster_runs(scope: PurgeScope) -> dict[str, int]:
    from pka.cli.purge_cluster_runs import purge_all_cluster_runs

    return purge_all_cluster_runs(dry_run=True)


def _purge_cluster_runs(scope: PurgeScope) -> dict[str, int]:
    from pka.cli.purge_cluster_runs import purge_all_cluster_runs

    return purge_all_cluster_runs(dry_run=False)


TARGETS: dict[str, PurgeTarget] = {
    t.key: t
    for t in (
        PurgeTarget(
            "summaries",
            "Generated summaries",
            2,
            _count_summaries,
            _purge_summaries,
            "POST /ingestion/enrich?kind=summary",
            provenance=True,
        ),
        PurgeTarget(
            "vectors",
            "Chunk and document embeddings",
            2,
            _count_vectors,
            _purge_vectors,
            "POST /ingestion/rebuild-vectors",
        ),
        PurgeTarget(
            "image_text",
            "Image descriptions, OCR and book extraction",
            2,
            _count_image_text,
            _purge_image_text,
            "POST /ingestion/sync/image",
        ),
        PurgeTarget(
            "clip_vectors",
            "CLIP image embeddings",
            2,
            _count_clip_vectors,
            _purge_clip_vectors,
            "POST /ingestion/sync/image",
        ),
        PurgeTarget(
            "machine_tags",
            "Machine-derived tags (LLM, cluster, inferred)",
            2,
            _count_machine_tags,
            _purge_machine_tags,
            "POST /clusters/run",
        ),
        PurgeTarget(
            "fetched_text",
            "Fetched body text",
            3,
            _count_fetched_text,
            _purge_fetched_text,
            "POST /ingestion/sync/{source}/ingest (re-fetches over the network)",
        ),
        PurgeTarget(
            "fetch_failures",
            "Unfetchable-URL records",
            3,
            _count_fetch_failures,
            _purge_fetch_failures,
            "POST /ingestion/sync/{source}/ingest",
        ),
        PurgeTarget(
            "cluster_runs",
            "Clustering runs",
            2,
            _count_cluster_runs,
            _purge_cluster_runs,
            "POST /clusters/run",
        ),
    )
}


def purge_target(
    key: str,
    *,
    source: str | None = None,
    dry_run: bool = False,
    run_id: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    unknown: bool = False,
) -> dict[str, int]:
    """Count (``dry_run``) or delete one registered target.

    Raises ``ValueError`` when a provenance filter is asked of a target whose
    artifact carries no run stamp: answering it by ignoring the filter would
    turn "purge what the old model made" into "purge everything".
    """
    if key not in TARGETS:
        raise ValueError(f"Unknown purge target {key!r}; expected one of {sorted(TARGETS)}")
    target = TARGETS[key]
    scope = PurgeScope(
        source=source, run_id=run_id, provider=provider, model=model, unknown=unknown
    )
    if scope.has_provenance and not target.provenance:
        raise ValueError(
            f"Target {key!r} records no provenance, so it cannot be filtered by "
            "run/provider/model. Stamped targets: "
            f"{sorted(t.key for t in TARGETS.values() if t.provenance)}"
        )
    return target.count(scope) if dry_run else target.purge(scope)


def describe_targets(source: str | None = None) -> list[dict]:
    """The registry plus live dry-run counts — what the UI enumerates."""
    scope = PurgeScope(source=source)
    return [
        {
            "key": t.key,
            "label": t.label,
            "tier": t.tier,
            "retrigger": t.retrigger,
            "provenance": t.provenance,
            "counts": t.count(scope),
        }
        for t in TARGETS.values()
    ]
