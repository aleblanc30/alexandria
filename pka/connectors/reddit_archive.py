"""On-disk archive of every Reddit feed poll — the backup the source cannot be.

Firefox, Zotero and Calibre each keep their own database, so a broken archive is
always re-readable from the original. Reddit is not like that: the saved list
lives on someone else's server, behind a token that can be rotated and a bot
filter that can start refusing us, and the feed only ever serves the newest
slice. Whatever a poll returned is, in practice, unrepeatable — so it is written
down before anything is parsed out of it.

Layout under ``data_dir/reddit``::

    reddit/
      20260819T140322Z/       # one poll
        page-01.xml           # the Atom document exactly as Reddit served it
        manifest.json         # when, how many pages, which item ids
      saved.jsonl             # cumulative, deduplicated item log

Two layers, because they answer different questions. The timestamped directories
are evidence: byte-identical pages, never revisited, which is what you want when
the question is "what changed since Tuesday" or "why did the parser produce
that". ``saved.jsonl`` is the restore path: one line per item, appended only
when the item is new or its content actually changed, so it accumulates without
duplicating and the last record for an id wins.

Deduplication is by content digest rather than by id alone. An id-only rule
would silently drop edits (a comment rewritten, a title changed); digests keep
that history while still making a re-run of an unchanged backfill append
nothing.

The feed URL is a credential and never appears here: only response bodies are
written, and manifests record no URL.

Nothing in this module is allowed to break a sync. Every write is best-effort
and reports failure through the log — an archive that took ingestion down with
it would defeat its own purpose.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from pka.config import settings as cfg

log = logging.getLogger(__name__)

_SAVED_LOG = "saved.jsonl"
_MANIFEST = "manifest.json"


def archive_root() -> Path:
    """Directory holding the poll snapshots and the cumulative log."""
    return cfg.data_dir / "reddit"


def _stamp(when: datetime) -> str:
    """UTC, compact, filename-safe — no colons (Windows rejects them in paths)."""
    return when.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _digest(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Cumulative log ───────────────────────────────────────────────────────────

def read_records(root: Path | None = None) -> list[dict]:
    """Every line of ``saved.jsonl``, oldest first. Unreadable lines are skipped.

    A half-written final line (killed mid-append) must not make the whole backup
    unreadable, which is the reason for JSON *Lines* rather than one JSON array.
    """
    path = (root or archive_root()) / _SAVED_LOG
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("Could not read the Reddit archive at %s: %s", path, exc)
        return []

    records: list[dict] = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            log.warning("Skipping unparseable line %d of %s", number, path)
    return records


def read_items(root: Path | None = None) -> list[dict]:
    """Archived items, one per id, newest version of each, in first-seen order."""
    latest: dict[str, dict] = {}
    for record in read_records(root):
        item = record.get("item")
        if isinstance(item, dict) and item.get("source_id"):
            latest[str(item["source_id"])] = item  # a later line wins
    return list(latest.values())


def _known_digests(root: Path) -> dict[str, str]:
    """id → digest of the most recent archived version, for the dedupe check."""
    known: dict[str, str] = {}
    for record in read_records(root):
        item = record.get("item")
        if isinstance(item, dict) and item.get("source_id"):
            known[str(item["source_id"])] = record.get("digest") or _digest(item)
    return known


def record_items(
    items: list[dict], when: datetime | None = None, root: Path | None = None,
) -> dict:
    """Append the items that are new or changed to ``saved.jsonl``.

    Returns ``{"new", "changed", "unchanged"}``. Never raises.
    """
    stats = {"new": 0, "changed": 0, "unchanged": 0}
    if not items:
        return stats
    directory = root or archive_root()
    when = when or datetime.now(UTC)

    try:
        known = _known_digests(directory)
        lines: list[str] = []
        for item in items:
            source_id = str(item.get("source_id") or "")
            if not source_id:
                continue
            digest = _digest(item)
            previous = known.get(source_id)
            if previous == digest:
                stats["unchanged"] += 1
                continue
            stats["changed" if previous else "new"] += 1
            known[source_id] = digest
            lines.append(json.dumps(
                {"archived_at": when.isoformat(), "digest": digest, "item": item},
                ensure_ascii=False, default=str,
            ))
        if lines:
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / _SAVED_LOG).open("a", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(lines) + "\n")
    except OSError as exc:
        log.warning("Could not append to the Reddit archive: %s", exc)
        return stats

    log.info(
        "Reddit archive: %d new, %d changed, %d unchanged",
        stats["new"], stats["changed"], stats["unchanged"],
    )
    return stats


# ── One poll ─────────────────────────────────────────────────────────────────

class PollArchive:
    """The snapshot directory for a single walk of the feed.

    Created lazily on the first page, so a poll that fails before Reddit answers
    leaves no empty directory behind. A poll that fails *after* some pages
    arrived keeps them: a partial walk is exactly when the bytes are worth
    having.
    """

    def __init__(self, when: datetime | None = None, root: Path | None = None):
        self.when = when or datetime.now(UTC)
        self.root = root or archive_root()
        self.dir: Path | None = None
        self.pages = 0

    def _ensure_dir(self) -> Path | None:
        if self.dir is not None:
            return self.dir
        base = self.root / _stamp(self.when)
        candidate, suffix = base, 1
        # Metadata and ingest can both poll within the same second; neither
        # snapshot may overwrite the other.
        while candidate.exists():
            suffix += 1
            candidate = base.with_name(f"{base.name}-{suffix}")
        try:
            candidate.mkdir(parents=True)
        except OSError as exc:
            log.warning("Could not create the Reddit poll archive %s: %s", candidate, exc)
            return None
        self.dir = candidate
        return candidate

    def add_page(self, text: str) -> None:
        """Write one raw feed response verbatim. Never raises."""
        directory = self._ensure_dir()
        if directory is None:
            return
        self.pages += 1
        try:
            (directory / f"page-{self.pages:02d}.xml").write_text(text, encoding="utf-8")
        except OSError as exc:
            log.warning("Could not archive Reddit feed page %d: %s", self.pages, exc)

    def finish(self, items: list[dict], error: str | None = None) -> None:
        """Append *items* to the cumulative log and write this poll's manifest.

        Called from a ``finally``, so *items* may be the partial result of a walk
        that then failed; ``error`` names that failure in the manifest.
        """
        if self.dir is None and not items:
            return
        stats = record_items(items, when=self.when, root=self.root)
        directory = self._ensure_dir()
        if directory is None:
            return
        manifest = {
            "polled_at": self.when.isoformat(),
            "pages": self.pages,
            "items": len(items),
            **stats,
            "source_ids": [i.get("source_id") for i in items],
        }
        if error:
            manifest["error"] = error
        try:
            (directory / _MANIFEST).write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("Could not write the Reddit poll manifest: %s", exc)
