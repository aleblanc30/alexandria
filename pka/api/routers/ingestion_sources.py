"""``/ingestion/sources`` — where each source is read from.

A single path per source, or for images a list of folders, plus the native
pickers that choose them.
"""

from fastapi import APIRouter, HTTPException

from pka.api import source_paths as spaths
from pka.api.ingestion_common import require_source
from pka.api.schemas.ingestion import SourcePathUpdate
from pka.constants import Source

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


# Declared before the ``/sources/{source}/…`` routes so the static ``image``
# segment is matched by these handlers rather than the single-path fallbacks.


@router.get("/sources/image/dirs")
async def get_image_dirs():
    return {"dirs": spaths.get_image_dirs()}


@router.post("/sources/image/dirs", status_code=200)
def add_image_dir(body: SourcePathUpdate):
    if not body.path.strip():
        raise HTTPException(400, "Path must not be empty")
    try:
        return {"dirs": spaths.add_image_dir(body.path)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/sources/image/dirs", status_code=200)
def remove_image_dir(body: SourcePathUpdate):
    if not body.path.strip():
        raise HTTPException(400, "Path must not be empty")
    try:
        return {"dirs": spaths.remove_image_dir(body.path)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/sources/image/dirs/browse", status_code=200)
def browse_image_dir():
    try:
        chosen = spaths.open_image_dir_picker()
    except RuntimeError as exc:
        raise HTTPException(501, str(exc)) from exc
    return {"path": chosen}


def _reject_image_single_path(source: str) -> None:
    """The image source is list-valued; steer callers to the ``/dirs`` routes."""
    if source == Source.IMAGE:
        raise HTTPException(400, "Image source uses /sources/image/dirs")


@router.get("/sources/{source}/path")
async def get_path(source: str):
    require_source(source)
    _reject_image_single_path(source)
    try:
        return spaths.get_source_path(source)
    except ValueError as exc:
        # Credential-based sources (e.g. Reddit) have no filesystem path.
        raise HTTPException(400, str(exc)) from exc


@router.put("/sources/{source}/path")
def update_path(source: str, body: SourcePathUpdate):
    require_source(source)
    _reject_image_single_path(source)
    if not body.path.strip():
        raise HTTPException(400, "Path must not be empty")
    try:
        return spaths.set_source_path(source, body.path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/sources/{source}/browse", status_code=200)
def browse_path(source: str):
    require_source(source)
    _reject_image_single_path(source)
    try:
        chosen = spaths.open_native_picker(source)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(501, str(exc)) from exc
    return {"path": chosen}
