"""Keyword search over the trigram FTS5 indexes the migrations build.

``documents_fts`` indexes ``documents.title`` + ``card_summary``; ``chunks_fts``
indexes ``chunks.text``, so a phrase is found anywhere in the archive: a PDF's
full text, a fetched page, an abstract, an image's OCR. Both are kept current by
triggers (``pka.db.migrate._fts_index``), so nothing here writes.

A query matches as one substring, case-insensitively, the way the title
``ILIKE '%q%'`` it replaces did, and in any script, since trigram needs no word
boundaries. Trigram cannot match fewer than three characters, so
:data:`MIN_QUERY_CHARS` is the caller's cue to fall back to a scan.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from pka.constants import Source

MIN_QUERY_CHARS = 3


def _phrase(query: str) -> str:
    """``query`` as one FTS5 phrase: quoted, with inner quotes doubled.

    Quoting makes every character literal, so a user's ``-``, ``*``, ``:`` or
    ``AND`` is searched for rather than parsed as FTS5 syntax.
    """
    return '"' + query.replace('"', '""') + '"'


def _sources_clause(sources: Sequence[Source | str] | None) -> tuple[str, dict]:
    if not sources:
        return "", {}
    names = [f"s{i}" for i in range(len(sources))]
    clause = " AND d.source IN (" + ", ".join(f":{n}" for n in names) + ")"
    return clause, {n: str(s) for n, s in zip(names, sources, strict=True)}


def keyword_document_ids(
    con: sa.Connection,
    query: str,
    sources: Sequence[Source | str] | None = None,
) -> list[int]:
    """Documents containing ``query``, best first.

    Title and card-summary matches come first, by BM25; documents matched only
    in their chunk bodies follow, by their best chunk's BM25. The two scores are
    kept apart rather than merged because they rank different things: a title
    that contains the phrase says more about a document than one paragraph of
    its body does. Ties go to the lower document id, so the order is stable.

    ``query`` must be at least :data:`MIN_QUERY_CHARS` long once stripped.
    """
    phrase = _phrase(query.strip())
    clause, params = _sources_clause(sources)
    params["q"] = phrase

    # The match runs in a MATERIALIZED CTE and the joins outside it: bm25() is
    # only valid on the row that the full-text scan itself produces, and once the
    # planner flattens a join or an aggregate into that scan, it refuses it.
    head = con.execute(
        sa.text(
            "WITH m AS MATERIALIZED ("
            "SELECT rowid AS id, bm25(documents_fts) AS score "
            "FROM documents_fts WHERE documents_fts MATCH :q) "
            "SELECT m.id FROM m JOIN documents d ON d.id = m.id "
            f"WHERE 1 = 1{clause} ORDER BY m.score, m.id"
        ),
        params,
    ).fetchall()
    body = con.execute(
        sa.text(
            "WITH m AS MATERIALIZED ("
            "SELECT rowid AS id, bm25(chunks_fts) AS score "
            "FROM chunks_fts WHERE chunks_fts MATCH :q) "
            "SELECT c.document_id, MIN(m.score) AS best "
            "FROM m JOIN chunks c ON c.id = m.id JOIN documents d ON d.id = c.document_id "
            f"WHERE 1 = 1{clause} GROUP BY c.document_id ORDER BY best, c.document_id"
        ),
        params,
    ).fetchall()

    ids = [r[0] for r in head]
    seen = set(ids)
    ids.extend(r[0] for r in body if r[0] not in seen)
    return ids
