"""Propose tag folds for review (DESIGN.md §3.8).

Case, accents and punctuation need no proposal: :func:`pka.db.tag_fold.tag_key`
folds them outright. What remains are decisions, and this module only proposes
them, as ``candidate`` rows in ``tag_aliases``; nothing folds until the user
accepts it. Three kinds:

- ``semantic``: two tags the archive's own embedding model places close
  together, in any of the archive's languages (`deep-learning` /
  `apprentissage-profond`). Computed locally with the model that embeds the
  chunks, so nothing leaves the machine.
- ``morphology``: a plural and its singular (`neural-networks` /
  `neural-network`), with a stop list for words that only look plural.
- ``initialism``: a short tag spelling the initials of a longer one that also
  exists (`ml` / `machine-learning`), which embeddings rate poorly.

A pair already proposed, merged or rejected is never proposed again. The more
used tag of a pair is its canonical side; for an initialism, the long form.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

import numpy as np
import sqlalchemy as sa

from pka.config import settings as cfg
from pka.db import engine, tag_aliases
from pka.db.tag_fold import UNALIASED_ORIGINS, fold_map, tag_key

log = logging.getLogger(__name__)

KINDS = ("semantic", "morphology", "initialism")

# Words that end like plurals and are not.
_NOT_PLURAL = frozenset(
    {
        "physics",
        "mathematics",
        "economics",
        "statistics",
        "linguistics",
        "politics",
        "ethics",
        "news",
        "series",
        "species",
        "analysis",
        "basis",
        "thesis",
        "crisis",
        "bus",
        "gas",
        "lens",
        "chaos",
        "cosmos",
        "corpus",
        "status",
        "virus",
        "glasses",
        "business",
        "process",
        "access",
        "class",
        "less",
    }
)


def tag_inventory() -> Counter[str]:
    """Documents per tag key, across every origin aliases apply to.

    Keys already folded away are left out: they are settled.
    """
    fm = fold_map()
    counts: Counter[str] = Counter()
    for origin, raws in fm.counts.items():
        if origin in UNALIASED_ORIGINS:
            continue
        for raw, n in raws.items():
            key = tag_key(raw)
            if key and key not in fm.aliases:
                counts[key] += n
    return counts


def _pair(a: str, b: str, counts: Counter[str], kind: str, score: float | None = None) -> dict:
    """The candidate row for *a* and *b*, the more used one canonical."""
    alias, canonical = sorted((a, b), key=lambda k: (-counts[k], k))[::-1]
    return {"alias": alias, "canonical": canonical, "kind": kind, "score": score}


def morphology_pairs(counts: Counter[str]) -> list[dict]:
    keys = set(counts)
    out = []
    for key in keys:
        head, _, last = key.rpartition("-")
        if last in _NOT_PLURAL or len(last) < 4:
            continue
        prefix = f"{head}-" if head else ""
        singulars = []
        if last.endswith("ies"):
            singulars.append(last[:-3] + "y")
        elif last.endswith(("ses", "xes", "ches", "shes")):
            singulars.append(last[:-2])
        elif last.endswith("s") and not last.endswith("ss"):
            singulars.append(last[:-1])
        for singular in singulars:
            other = prefix + singular
            if other in keys:
                out.append(_pair(key, other, counts, "morphology"))
    return out


def initialism_pairs(counts: Counter[str]) -> list[dict]:
    short = {k for k in counts if "-" not in k and 2 <= len(k) <= 5 and k.isalpha()}
    by_initials: dict[str, list[str]] = {}
    for key in counts:
        words = [w for w in key.split("-") if w]
        if len(words) >= 2:
            by_initials.setdefault("".join(w[0] for w in words), []).append(key)
    out = []
    for s in short:
        for long_form in by_initials.get(s, ()):
            out.append({"alias": s, "canonical": long_form, "kind": "initialism", "score": None})
    return out


def semantic_pairs(
    counts: Counter[str],
    threshold: float,
    *,
    embedder: Any = None,
    block: int = 512,
) -> list[dict]:
    """Pairs whose embeddings' cosine similarity is at least *threshold*.

    Only the ``tag_dedup_max_tags`` most used tags with at least
    ``tag_dedup_min_documents`` documents are compared, in blocks, so memory
    stays at ``block`` × that many similarities.
    """
    keys = [
        k for k, n in counts.most_common(cfg.tag_dedup_max_tags) if n >= cfg.tag_dedup_min_documents
    ]
    if len(keys) < 2:
        return []
    if embedder is None:
        from pka.storage.vector_store import active_embedder

        embedder = active_embedder()
    # Symmetric comparison: every tag is embedded the same way, as a passage.
    vectors = np.asarray(
        embedder.embed_documents([k.replace("-", " ") for k in keys]), dtype=np.float32
    )
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    unit = (vectors / np.where(norms == 0, 1, norms)).astype(np.float32)

    out = []
    for start in range(0, len(keys), block):
        sims = unit[start : start + block] @ unit.T
        rows, cols = np.nonzero(sims >= threshold)
        for r, c in zip(rows.tolist(), cols.tolist(), strict=True):
            i = start + r
            if c <= i:
                continue
            out.append(_pair(keys[i], keys[c], counts, "semantic", round(float(sims[r, c]), 4)))
    return out


def scan(
    kinds: tuple[str, ...] = KINDS,
    *,
    threshold: float | None = None,
    dry_run: bool = False,
    embedder: Any = None,
) -> dict[str, Any]:
    """Propose new fold candidates; returns per-kind counts and the new pairs.

    With ``dry_run``, reports what it would propose and writes nothing.
    """
    counts = tag_inventory()
    limit = cfg.tag_dedup_similarity if threshold is None else threshold
    found: list[dict] = []
    if "morphology" in kinds:
        found += morphology_pairs(counts)
    if "initialism" in kinds:
        found += initialism_pairs(counts)
    if "semantic" in kinds:
        found += semantic_pairs(counts, limit, embedder=embedder)

    with engine.get_engine().connect() as con:
        known = tag_aliases.known_pairs(con)
    seen: set[frozenset[str]] = set()
    fresh = []
    for row in found:
        pair = frozenset((row["alias"], row["canonical"]))
        if pair in known or pair in seen:
            continue
        seen.add(pair)
        fresh.append(row)

    added = len(fresh) if dry_run else tag_aliases.add_candidates(fresh)
    by_kind = Counter(r["kind"] for r in fresh)
    log.info("Tag scan over %d tags: %d new candidate(s) %s", len(counts), added, dict(by_kind))
    return {
        "tags": len(counts),
        "proposed": added,
        "by_kind": dict(by_kind),
        "dry_run": dry_run,
        "pairs": fresh,
    }


def variant_report() -> list[dict[str, Any]]:
    """Groups of stored spellings :func:`tag_key` already folds, largest first."""
    fm = fold_map()
    groups: dict[tuple[str, str], list[str]] = {}
    for origin, raws in fm.counts.items():
        for raw in raws:
            groups.setdefault((origin, fm.canonical(raw, origin)), []).append(raw)
    out = [
        {
            "origin": origin,
            "tag": fm.display(members[0], origin),
            "variants": fm.group(members[0], origin),
            "documents": sum(fm.counts[origin][m] for m in members),
        }
        for (origin, _), members in groups.items()
        if len(members) > 1
    ]
    return sorted(out, key=lambda g: (-len(g["variants"]), -g["documents"], g["tag"]))


def describe(rows: list[dict]) -> list[dict[str, Any]]:
    """Alias rows with display forms, document counts and example titles, for review."""
    from pka.db.schema import documents, overlay_tags, source_tags

    fm = fold_map()
    display: dict[str, tuple[str, int]] = {}
    for raws in fm.counts.values():
        for raw, n in raws.items():
            key = tag_key(raw)
            best = display.get(key)
            if best is None or n > best[1]:
                display[key] = (raw, n)
    totals: Counter[str] = Counter()
    for origin, raws in fm.counts.items():
        if origin in UNALIASED_ORIGINS:
            continue
        for raw, n in raws.items():
            totals[tag_key(raw)] += n

    def examples(key: str) -> list[str]:
        raws = sorted({r for raws in fm.counts.values() for r in raws if tag_key(r) == key})
        if not raws:
            return []
        with engine.get_engine().connect() as con:
            doc_ids = sa.union(
                sa.select(source_tags.c.document_id).where(source_tags.c.tag_string.in_(raws)),
                sa.select(overlay_tags.c.document_id).where(overlay_tags.c.tag.in_(raws)),
            ).subquery()
            return [
                r[0]
                for r in con.execute(
                    sa.select(documents.c.title)
                    .where(documents.c.id.in_(sa.select(doc_ids.c.document_id)))
                    .where(documents.c.title.isnot(None))
                    .order_by(documents.c.id)
                    .limit(3)
                )
            ]

    out = []
    for row in rows:
        side = {}
        for name in ("alias", "canonical"):
            key = row[name]
            side[name] = {
                "key": key,
                "label": display.get(key, (key, 0))[0],
                "documents": totals.get(key, 0),
                "examples": examples(key) if row["state"] == "candidate" else [],
            }
        out.append({**row, **side})
    return out
