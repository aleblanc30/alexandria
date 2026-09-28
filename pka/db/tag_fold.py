"""Tag folding: which stored tag strings read as one tag (DESIGN.md §3.8).

Sources write tags verbatim, so one idea arrives as `Machine Learning`,
`machine-learning` and `Machine learning`, or as `Économie` and `economie`.
Nothing rewrites them. Two layers decide, at read time, which strings are one
tag:

1. :func:`tag_key`, a normalisation with no judgement in it: case, accents,
   punctuation and spacing.
2. ``tag_aliases``, the decisions: an active row folds one key into another
   (`ml` into `machine-learning`, `apprentissage-profond` into
   `deep-learning`). Proposing them is :mod:`pka.tag_dedup`'s job.

Folding stays inside an origin: a source tag and a cluster label that fold to
the same key remain two tags, because the origins make different claims.
``inferred`` tags are a closed vocabulary their writers recreate on every run,
so aliases do not apply to them; the key still does.

:func:`fold_map` is the view every read site uses. It is rebuilt from the
distinct tag strings at most every :data:`_TTL_SECONDS`, and at once after an
alias changes.
"""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import sqlalchemy as sa

from pka.constants import TagOrigin
from pka.db import engine
from pka.db.schema import overlay_tags, source_tags, tag_aliases

SOURCE_ORIGIN = str(TagOrigin.SOURCE)
#: Origins aliases do not apply to (closed vocabularies).
UNALIASED_ORIGINS = frozenset({str(TagOrigin.INFERRED)})

_PUNCT_RE = re.compile(r"[^\w\s-]", re.UNICODE)
_SPACE_RE = re.compile(r"[\s_]+")
_HYPHEN_RE = re.compile(r"-+")


def tag_key(raw: str) -> str:
    """The normalised form of a tag: lowercase, no accents, hyphen-separated."""
    text = unicodedata.normalize("NFKD", raw or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower().strip()
    text = _PUNCT_RE.sub("", text)
    return _HYPHEN_RE.sub("-", _SPACE_RE.sub("-", text)).strip("-")[:64]


@dataclass
class FoldMap:
    """Stored tag strings and the tag each reads as, per origin."""

    aliases: dict[str, str]
    #: origin -> raw string -> documents carrying it
    counts: dict[str, Counter[str]]
    _groups: dict[tuple[str, str], list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for origin, raws in self.counts.items():
            for raw in raws:
                self._groups.setdefault((origin, self.canonical(raw, origin)), []).append(raw)

    def canonical(self, raw: str, origin: str | None = None) -> str:
        """The key *raw* folds to in *origin*."""
        key = tag_key(raw) or raw
        if origin in UNALIASED_ORIGINS:
            return key
        return self.aliases.get(key, key)

    def variants(self, tag: str, origin: str | None = None) -> list[str]:
        """Every stored string that reads as *tag*, in *origin* or in any overlay origin.

        *tag* itself is always included, so a string the map has not seen yet
        (written since it was built) still matches itself.
        """
        origins = [origin] if origin else [o for o in self.counts if o != SOURCE_ORIGIN]
        out = {tag}
        for o in origins:
            out.update(self._groups.get((o, self.canonical(tag, o)), ()))
        return sorted(out)

    def display(self, raw: str, origin: str) -> str:
        """The form shown for *raw*'s tag: its most used variant, ties alphabetical."""
        group = self._groups.get((origin, self.canonical(raw, origin)))
        if not group:
            return raw
        counts = self.counts[origin]
        return min(group, key=lambda s: (-counts[s], s))

    def group(self, raw: str, origin: str) -> list[str]:
        """The variants of *raw*'s tag in *origin*, most used first."""
        group = self._groups.get((origin, self.canonical(raw, origin)), [raw])
        counts = self.counts.get(origin, Counter())
        return sorted(group, key=lambda s: (-counts[s], s))


_TTL_SECONDS = 30.0
_cache: tuple[float, FoldMap] | None = None
_lock = threading.Lock()


def active_aliases(con: sa.Connection) -> dict[str, str]:
    return {
        r[0]: r[1]
        for r in con.execute(
            sa.select(tag_aliases.c.alias, tag_aliases.c.canonical).where(
                tag_aliases.c.state == "active"
            )
        )
    }


def _build() -> FoldMap:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    with engine.get_engine().connect() as con:
        for tag, n in con.execute(
            sa.select(
                source_tags.c.tag_string, sa.func.count(sa.distinct(source_tags.c.document_id))
            ).group_by(source_tags.c.tag_string)
        ):
            counts[SOURCE_ORIGIN][tag] = n
        for tag, origin, n in con.execute(
            sa.select(
                overlay_tags.c.tag,
                overlay_tags.c.origin,
                sa.func.count(sa.distinct(overlay_tags.c.document_id)),
            ).group_by(overlay_tags.c.tag, overlay_tags.c.origin)
        ):
            counts[origin][tag] = n
        aliases = active_aliases(con)
    return FoldMap(aliases=aliases, counts=dict(counts))


def fold_map() -> FoldMap:
    """The current fold map, rebuilt when older than :data:`_TTL_SECONDS`."""
    global _cache
    with _lock:
        now = time.monotonic()
        if _cache is None or now - _cache[0] > _TTL_SECONDS:
            _cache = (now, _build())
        return _cache[1]


def invalidate() -> None:
    """Drop the cached map: the next read rebuilds it."""
    global _cache
    with _lock:
        _cache = None
