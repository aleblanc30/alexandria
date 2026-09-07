"""``/search`` endpoint — semantic, fulltext, and hybrid modes.

The stages themselves live in :mod:`pka.api.search_hits`; this module owns the
HTTP surface and the order they run in.

The N+1 query problem in the per-document lookup loop is avoided by
:func:`pka.api.document_serialize.documents_out_batch`, which collects all
required relations (``source_tags``, ``overlay_tags``, ``cluster_assignments``)
in batched ``IN`` queries.
"""

import logging

from fastapi import APIRouter, Depends

from pka.api.active_run import fetch_active_run_id
from pka.api.dependencies import get_engine
from pka.api.document_serialize import documents_out_batch
from pka.api.schemas.search import SearchRequest, SearchResponse
from pka.api.search_hits import (
    Hits,
    apply_browse_filters,
    apply_row_filters,
    fulltext_hits,
    merge_clip_hits,
    merge_new,
    semantic_hits,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])


@router.post("", response_model=SearchResponse)
def search(req: SearchRequest, engine=Depends(get_engine)):
    results: Hits = []
    if req.mode in ("semantic", "hybrid"):
        results = semantic_hits(req)

    with engine.connect() as con:
        run_id = fetch_active_run_id(con)

        # An empty ``results`` here means the semantic stage found nothing or the
        # vector store was unavailable; either way fulltext is the fallback.
        if req.mode in ("fulltext", "hybrid") or not results:
            results = merge_new(results, fulltext_hits(con, req))

        results = merge_clip_hits(results, req)
        results = apply_browse_filters(con, results, req)
        results = apply_row_filters(con, results, req, run_id)

        total = len(results)
        page = results[req.offset : req.offset + req.limit]
        docs_out = documents_out_batch(page, con, run_id)

    return SearchResponse(query=req.query, total=total, documents=docs_out)
