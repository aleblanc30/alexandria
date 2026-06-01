"""
Read-only Firefox bookmarks connector.

Dev (``PKA_DEV=1``): snapshot ``places.sqlite`` once into ``data/`` and reuse it.
Prod: read the live profile DB directly (may wait on Firefox's lock).
Bookmark *content* (HTTP fetch + parse) is deferred to :mod:`pka.ingestion.fetcher`.
"""
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pka.config import settings as cfg
from pka.db.sqlite_copy import dev_sqlite_snapshot

log = logging.getLogger(__name__)

LIVE_DB_TIMEOUT_SECONDS = 30.0


@dataclass
class FirefoxBookmark:
    source_id: str          # moz_bookmarks.id as string
    url: str
    title: str
    folder_path: str        # e.g. "Research/Distributed Systems"
    tags: list[str]         # tags assigned inside Firefox
    date_added: int | None  # unix timestamp (µs → s conversion applied)


# ── Profile detection ────────────────────────────────────────────────────────

def _find_places_sqlite(firefox_root: Path) -> Path:
    """Locate ``places.sqlite`` inside the default-release profile, falling back
    to the first profile that contains the file."""
    matches = sorted(firefox_root.glob("*.default-release/places.sqlite"))
    if matches:
        return matches[0]

    all_matches = sorted(firefox_root.glob("*/places.sqlite"))
    if all_matches:
        log.debug("Using Firefox profile: %s", all_matches[0].parent.name)
        return all_matches[0]

    raise FileNotFoundError(
        f"Could not find places.sqlite under {firefox_root}. "
        "Set PKA_FIREFOX_DB to the exact path if the profile is non-standard."
    )


def _resolve_places_db(src: Path, *, refresh: bool = False) -> Path:
    if cfg.dev:
        return dev_sqlite_snapshot(
            src, cfg.firefox_places_copy, label="Firefox places", refresh=refresh,
        )
    return src


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.resolve()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=LIVE_DB_TIMEOUT_SECONDS)


# ── Folder path reconstruction ───────────────────────────────────────────────

def _build_folder_index(cur: sqlite3.Cursor) -> dict[int, str]:
    """Iterative path reconstruction — no recursion, no cycle risk."""
    cur.execute("""
        SELECT id, parent, title
        FROM   moz_bookmarks
        WHERE  type = 2          -- 2 = folder
    """)
    rows = cur.fetchall()
    parents: dict[int, int] = {r["id"]: r["parent"] for r in rows}
    titles:  dict[int, str] = {r["id"]: r["title"] or "" for r in rows}

    cache: dict[int, str] = {}
    MAX_DEPTH = 32

    for start in parents:
        if start in cache:
            continue
        chain: list[int] = []
        cur_id = start
        visited: set[int] = set()
        # Walk up, collecting unresolved ancestors
        while cur_id and cur_id not in cache and cur_id not in visited:
            visited.add(cur_id)
            chain.append(cur_id)
            if len(chain) > MAX_DEPTH:
                break
            cur_id = parents.get(cur_id, 0)
            if cur_id == 0:
                break

        # Resolve from the deepest already-cached prefix outward
        base = cache.get(cur_id, "")
        for fid in reversed(chain):
            own = titles.get(fid, "")
            base = f"{base}/{own}" if base else own
            cache[fid] = base

    return cache


# ── Tag extraction ───────────────────────────────────────────────────────────

def _build_tag_index(cur: sqlite3.Cursor) -> dict[int, list[str]]:
    """Return ``{place_id: [tag, ...]}`` using Firefox's tag mechanism.

    Firefox stores tags as bookmark entries inside special tag-folders living
    under the Tags root (``guid = 'tags________'``).
    """
    cur.execute("SELECT id FROM moz_bookmarks WHERE guid = 'tags________'")
    row = cur.fetchone()
    if not row:
        return {}
    tags_root_id: int = row["id"]

    cur.execute("""
        SELECT id, title
        FROM   moz_bookmarks
        WHERE  parent = ? AND type = 2
    """, (tags_root_id,))
    tag_folders = {r["id"]: r["title"] for r in cur.fetchall()}

    idx: dict[int, list[str]] = {}
    for folder_id, tag_name in tag_folders.items():
        cur.execute("""
            SELECT fk AS place_id
            FROM   moz_bookmarks
            WHERE  parent = ? AND type = 1
        """, (folder_id,))
        for r in cur.fetchall():
            idx.setdefault(r["place_id"], []).append(tag_name)

    return idx


# ── Main loader ──────────────────────────────────────────────────────────────

def load_bookmarks(
    firefox_root: Path | None = None,
    places_db: Path | None = None,
    *,
    refresh: bool = False,
) -> list[FirefoxBookmark]:
    """Load bookmarks from Firefox ``places.sqlite``.

    Resolution priority:
      1. ``places_db`` argument (exact path)
      2. ``firefox_root`` argument  → auto-detect profile
      3. :data:`pka.config.settings.firefox_db` → auto-detect profile

    When ``PKA_DEV=1``, the profile DB is copied once to ``data/firefox_places_copy.sqlite``
    and that snapshot is reused. Pass ``refresh=True`` (or delete the copy) to resnapshot.
    In production, the live profile file is opened read-only.
    """
    if places_db:
        db_path = places_db
        if not db_path.exists():
            raise FileNotFoundError(f"Firefox places.sqlite not found: {db_path}")
    else:
        root = firefox_root or cfg.firefox_db
        src = _find_places_sqlite(root) if root.is_dir() else root
        if not src.exists():
            raise FileNotFoundError(f"Firefox places.sqlite not found: {src}")
        db_path = _resolve_places_db(src, refresh=refresh)

    bookmarks: list[FirefoxBookmark] = []

    with _connect_ro(db_path) as con:
        con.row_factory = sqlite3.Row
        cur = con.cursor()

        folder_index = _build_folder_index(cur)
        tag_index    = _build_tag_index(cur)

        cur.execute("""
            SELECT
                b.id,
                b.parent,
                b.title          AS bm_title,
                b.dateAdded      AS date_added,
                p.url,
                p.id             AS place_id,
                p.title          AS page_title
            FROM   moz_bookmarks b
            JOIN   moz_places p ON b.fk = p.id
            WHERE  b.type = 1
              AND  p.url NOT LIKE 'place:%'
              AND  p.url NOT LIKE 'javascript:%'
        """)
        rows = cur.fetchall()
        log.info("Found %d Firefox bookmark entries", len(rows))

        seen_urls: set[str] = set()

        for row in rows:
            url = row["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)

            da_us = row["date_added"]
            date_added = int(da_us / 1_000_000) if da_us else None

            title = (
                (row["bm_title"] or "").strip()
                or (row["page_title"] or "").strip()
                or url
            )

            folder_path = folder_index.get(row["parent"], "")
            tags = tag_index.get(row["place_id"], [])

            bookmarks.append(FirefoxBookmark(
                source_id   = str(row["id"]),
                url         = url,
                title       = title,
                folder_path = folder_path,
                tags        = tags,
                date_added  = date_added,
            ))

    log.info("Loaded %d unique bookmarks", len(bookmarks))
    return bookmarks
