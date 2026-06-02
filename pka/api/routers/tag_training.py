"""``/tag-training`` — active learning tag classifier sessions."""
from fastapi import APIRouter, HTTPException

from pka.api.schemas.tag_training import (
    LabelsBatch,
    PseudoLabelRequest,
    QueueDocOut,
    SessionCreate,
    SessionFromSourceTag,
    SessionOut,
)
from pka.tag_training import lifecycle

router = APIRouter(prefix="/tag-training", tags=["tag_training"])


def _session_out(data: dict) -> SessionOut:
    return SessionOut(**{k: v for k, v in data.items() if k in SessionOut.model_fields})


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions():
    return [_session_out(s) for s in lifecycle.list_sessions()]


@router.post("/sessions", response_model=SessionOut)
async def create_session(req: SessionCreate):
    try:
        data = lifecycle.create_session(
            req.tag,
            [item.model_dump() for item in req.labels],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _session_out(data)


@router.post("/sessions/from-source-tag", response_model=SessionOut)
async def create_from_source_tag(req: SessionFromSourceTag):
    try:
        data = lifecycle.create_session_from_source_tag(
            req.source_tag, req.target_tag,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _session_out(data)


@router.get("/sessions/by-tag/{tag}", response_model=SessionOut)
async def get_session_by_tag(tag: str):
    data = lifecycle.find_resumable_session_for_tag(tag)
    if data is None:
        raise HTTPException(404, "No training session for this tag")
    return _session_out(data)


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get_session(session_id: int):
    try:
        return _session_out(lifecycle.get_session(session_id))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/sessions/{session_id}/queue", response_model=list[QueueDocOut])
async def get_queue(session_id: int):
    try:
        lifecycle.get_session(session_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return [QueueDocOut(**row) for row in lifecycle.get_queue(session_id)]


@router.post("/sessions/{session_id}/labels", response_model=SessionOut)
async def post_labels(session_id: int, req: LabelsBatch):
    try:
        data = lifecycle.add_user_labels(
            session_id,
            [item.model_dump() for item in req.labels],
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _session_out(data)


@router.post("/sessions/{session_id}/pseudo-label", response_model=SessionOut)
async def post_pseudo_label(session_id: int, req: PseudoLabelRequest):
    try:
        if req.mode == "model":
            data = lifecycle.apply_pseudo_labels_model(session_id)
        elif req.mode == "llm":
            data = lifecycle.apply_pseudo_labels_llm(
                session_id, batch_size=req.batch_size,
            )
        else:
            raise HTTPException(400, "mode must be 'model' or 'llm'")
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _session_out(data)


@router.post("/sessions/{session_id}/train", response_model=SessionOut)
async def post_train(session_id: int):
    try:
        data = lifecycle.train_session(session_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return _session_out(data)


@router.post("/sessions/{session_id}/resume", response_model=SessionOut)
async def post_resume(session_id: int):
    try:
        data = lifecycle.resume_session(session_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _session_out(data)


@router.post("/sessions/{session_id}/accept", response_model=SessionOut)
async def post_accept(session_id: int):
    try:
        data = lifecycle.accept_session(session_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _session_out(data)


@router.post("/sessions/{session_id}/archive", response_model=SessionOut)
async def post_archive(session_id: int):
    try:
        return _session_out(lifecycle.archive_session(session_id))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
