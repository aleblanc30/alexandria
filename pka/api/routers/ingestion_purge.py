"""``/ingestion`` — purges, and the enrichment runs they can target.

A purge removes a whole source, or one registered target narrowed by source
and provenance (``run_id`` / ``provider`` / ``model``).
"""

from fastapi import APIRouter, HTTPException

from pka.api.ingestion_common import require_nothing_running, require_source, seed_baselines
from pka.constants import ALL_SOURCES
from pka.ingestion import progress as sp

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


@router.post("/sources/{source}/purge")
def purge_source_endpoint(source: str, include_user_data: bool = False):
    """Delete every archived row (and vectors) for ``source``.

    By default, manually-applied/learned tags and reading-list entries survive:
    they are user-authored, and no re-ingest can bring them back. Pass
    ``include_user_data=true`` to remove those too. Refuses while a sync is running so a purge can't race
    a live worker.
    """
    require_source(source)
    if sp.is_running(source):
        raise HTTPException(409, f"Stop the running sync for {source} before purging")

    from pka.cli.purge_source import purge_source
    from pka.db.migrate import init_db

    init_db()
    counts = purge_source(source, include_user_data=include_user_data)
    sp.reset(source)
    seed_baselines(source)
    return {"status": "purged", "source": source, "counts": counts}


@router.get("/purge-targets")
def purge_targets(source: str | None = None):
    """The purge registry with live dry-run counts — one row per button."""
    if source:
        require_source(source)
    from pka.db.migrate import init_db
    from pka.purge import describe_targets

    init_db()
    return {"source": source, "targets": describe_targets(source)}


@router.get("/enrichment-runs")
def enrichment_runs_list(kind: str | None = None, limit: int = 100):
    """What ran, when, with which model, and at what cost in provider traffic.

    The provenance surface behind the purge filters below: a run listed here is
    a `run_id` a purge can target.
    """
    from pka.db.migrate import init_db
    from pka.enrichment_runs import list_runs

    if not 1 <= limit <= 500:
        raise HTTPException(400, "limit must be between 1 and 500")
    init_db()
    return {"runs": list_runs(kind=kind, limit=limit)}


@router.post("/purge/{key}")
def purge_target_endpoint(
    key: str,
    source: str | None = None,
    dry_run: bool = False,
    run_id: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    unknown: bool = False,
):
    """Purge one registered target, optionally scoped by source and provenance.

    ``run_id`` / ``provider`` / ``model`` narrow to what a particular backend
    produced; ``unknown=true`` selects the pre-provenance backlog. Targets whose
    artifact carries no run stamp reject those filters with a 400 rather than
    silently widening the purge.
    """
    if source:
        require_source(source)
    from pka.db.migrate import init_db
    from pka.purge import purge_target

    if not dry_run:
        require_nothing_running(source)

    init_db()
    try:
        counts = purge_target(
            key,
            source=source,
            dry_run=dry_run,
            run_id=run_id,
            provider=provider,
            model=model,
            unknown=unknown,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if not dry_run:
        # Counts the status view reports just changed under it.
        for src in [source] if source else ALL_SOURCES:
            seed_baselines(src)
    return {
        "status": "counted" if dry_run else "purged",
        "target": key,
        "source": source,
        "counts": counts,
    }
