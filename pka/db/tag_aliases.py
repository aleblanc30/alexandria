"""Writes to ``tag_aliases``: proposing, merging, rejecting and undoing folds.

Every write keeps the fold one hop deep: a key is never folded into a key that
is itself folded away, so a read resolves any tag with one dictionary lookup
(:mod:`pka.db.tag_fold`).
"""

from __future__ import annotations

import time
from typing import Any

import sqlalchemy as sa

from pka.db import engine, tag_fold
from pka.db.schema import tag_aliases
from pka.db.tag_fold import tag_key

KINDS = ("semantic", "morphology", "initialism", "manual")
STATES = ("candidate", "active", "rejected")


class AliasError(ValueError):
    """A merge that cannot be made: a key into itself, or a cycle."""


def _row(r: sa.Row) -> dict[str, Any]:
    return dict(r._mapping)


def list_aliases(state: str | None = None) -> list[dict[str, Any]]:
    q = sa.select(tag_aliases)
    if state:
        q = q.where(tag_aliases.c.state == state)
    q = q.order_by(tag_aliases.c.score.desc().nulls_last(), tag_aliases.c.id)
    with engine.get_engine().connect() as con:
        return [_row(r) for r in con.execute(q)]


def known_pairs(con: sa.Connection) -> set[frozenset[str]]:
    """Every pair already proposed, merged or rejected, in either direction."""
    return {
        frozenset((r[0], r[1]))
        for r in con.execute(sa.select(tag_aliases.c.alias, tag_aliases.c.canonical))
    }


def add_candidates(rows: list[dict[str, Any]]) -> int:
    """Insert ``{alias, canonical, kind, score}`` proposals not already known."""
    if not rows:
        return 0
    now = int(time.time())
    added = 0
    with engine.get_engine().begin() as con:
        known = known_pairs(con)
        for row in rows:
            pair = frozenset((row["alias"], row["canonical"]))
            if len(pair) < 2 or pair in known:
                continue
            con.execute(
                tag_aliases.insert().values(
                    alias=row["alias"],
                    canonical=row["canonical"],
                    kind=row["kind"],
                    score=row.get("score"),
                    state="candidate",
                    decided_by="scan",
                    created_at=now,
                )
            )
            known.add(pair)
            added += 1
    return added


def merge(alias: str, canonical: str, *, kind: str = "manual") -> dict[str, Any]:
    """Fold tag *alias* into tag *canonical*; both may be any stored spelling.

    The canonical side resolves through existing folds first, and every key
    already folded into *alias* is repointed at the new canonical, so the
    table stays one hop deep. A pair that exists in another state becomes
    active. Returns the active row.
    """
    a, c = tag_key(alias), tag_key(canonical)
    if not a or not c:
        raise AliasError("a tag with no letters or digits cannot be merged")
    now = int(time.time())
    with engine.get_engine().begin() as con:
        active = tag_fold.active_aliases(con)
        c = active.get(c, c)
        if a == c:
            raise AliasError(f"{alias!r} and {canonical!r} are already the same tag")
        # `a` may be active under another canonical: that fold is replaced.
        con.execute(
            tag_aliases.update()
            .where((tag_aliases.c.alias == a) & (tag_aliases.c.state == "active"))
            .values(state="rejected", decided_by="user", decided_at=now)
        )
        # Keys folded into `a` follow it into `c`.
        for folded in [k for k, v in active.items() if v == a]:
            _activate(con, folded, c, "manual", now)
        row = _activate(con, a, c, kind, now)
    tag_fold.invalidate()
    return row


def _activate(con: sa.Connection, alias: str, canonical: str, kind: str, now: int) -> dict:
    if alias == canonical:
        return {}
    con.execute(
        tag_aliases.update()
        .where((tag_aliases.c.alias == alias) & (tag_aliases.c.state == "active"))
        .values(state="rejected", decided_by="user", decided_at=now)
    )
    existing = con.execute(
        sa.select(tag_aliases.c.id).where(
            (tag_aliases.c.alias == alias) & (tag_aliases.c.canonical == canonical)
        )
    ).scalar()
    values = {"state": "active", "decided_by": "user", "decided_at": now}
    if existing is None:
        result = con.execute(
            tag_aliases.insert().values(
                alias=alias, canonical=canonical, kind=kind, created_at=now, **values
            )
        )
        assert result.inserted_primary_key is not None
        existing = result.inserted_primary_key[0]
    else:
        con.execute(tag_aliases.update().where(tag_aliases.c.id == existing).values(**values))
    return _row(con.execute(sa.select(tag_aliases).where(tag_aliases.c.id == existing)).one())


def accept(alias_id: int) -> dict[str, Any]:
    """Make candidate *alias_id* an active fold."""
    with engine.get_engine().connect() as con:
        row = con.execute(sa.select(tag_aliases).where(tag_aliases.c.id == alias_id)).first()
    if row is None:
        raise KeyError(alias_id)
    return merge(row.alias, row.canonical, kind=row.kind)


def reject(alias_id: int) -> bool:
    """Decline a proposal, or undo a fold; either way it is not proposed again."""
    with engine.get_engine().begin() as con:
        n = con.execute(
            tag_aliases.update()
            .where(tag_aliases.c.id == alias_id)
            .values(state="rejected", decided_by="user", decided_at=int(time.time()))
        ).rowcount
    tag_fold.invalidate()
    return bool(n)


def unmerge(alias: str) -> bool:
    """Undo the active fold of tag *alias*, remembered as rejected."""
    with engine.get_engine().begin() as con:
        n = con.execute(
            tag_aliases.update()
            .where((tag_aliases.c.alias == tag_key(alias)) & (tag_aliases.c.state == "active"))
            .values(state="rejected", decided_by="user", decided_at=int(time.time()))
        ).rowcount
    tag_fold.invalidate()
    return bool(n)
