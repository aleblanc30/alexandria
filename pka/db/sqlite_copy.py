"""Consistent SQLite snapshots for read-only connectors."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Avoid parallel backups of the same destination and hammering live source DBs.
_dst_locks: dict[str, threading.Lock] = {}
_dst_locks_guard = threading.Lock()
_last_copy_at: dict[str, float] = {}

DEFAULT_COPY_MIN_INTERVAL_SECONDS = 60.0
DEFAULT_COPY_TIMEOUT_SECONDS = 120.0


def _dst_lock(dst: Path) -> threading.Lock:
    key = str(dst.resolve())
    with _dst_locks_guard:
        if key not in _dst_locks:
            _dst_locks[key] = threading.Lock()
        return _dst_locks[key]


def _unlink_sqlite_files(path: Path) -> None:
    path.unlink(missing_ok=True)
    Path(f"{path}-wal").unlink(missing_ok=True)
    Path(f"{path}-shm").unlink(missing_ok=True)


def _newest_mtime(path: Path) -> float | None:
    best: float | None = None
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        best = mtime if best is None else max(best, mtime)
    return best


def copy_sqlite_database(
    src: Path,
    dst: Path,
    *,
    timeout: float = DEFAULT_COPY_TIMEOUT_SECONDS,
) -> Path:
    """Snapshot ``src`` into ``dst`` using SQLite's online backup API.

    Safe while another process (Firefox, Zotero, Calibre) holds the database
    open in WAL mode. Replaces ``shutil.copy`` of ``.sqlite`` + ``-wal``/``-shm``
    siblings, which can produce a malformed image when files change mid-copy.
    """
    src = src.resolve()
    dst = dst.resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)

    tmp = dst.with_name(f"{dst.name}.tmp")
    _unlink_sqlite_files(tmp)

    uri = f"file:{src}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=timeout) as src_con:
            with sqlite3.connect(tmp) as dst_con:
                src_con.backup(dst_con)
                dst_con.execute("PRAGMA journal_mode=DELETE")
                dst_con.commit()
    except sqlite3.Error:
        _unlink_sqlite_files(tmp)
        raise

    _unlink_sqlite_files(dst)
    tmp.replace(dst)
    log.debug("SQLite backup %s -> %s", src, dst)
    return dst


def ensure_sqlite_copy(
    src: Path,
    dst: Path,
    *,
    timeout: float = DEFAULT_COPY_TIMEOUT_SECONDS,
    min_interval_seconds: float = DEFAULT_COPY_MIN_INTERVAL_SECONDS,
    refresh: bool = False,
) -> Path:
    """Return a consistent read-only copy, reusing a recent snapshot when possible."""
    src = src.resolve()
    dst = dst.resolve()
    lock = _dst_lock(dst)
    with lock:
        if not refresh and dst.exists():
            elapsed = time.monotonic() - _last_copy_at.get(str(dst), 0.0)
            if elapsed < min_interval_seconds:
                log.debug("Reusing SQLite copy (%.0fs old): %s", elapsed, dst)
                return dst
            src_mtime = _newest_mtime(src)
            if src_mtime is not None and dst.stat().st_mtime >= src_mtime:
                log.debug("Reusing SQLite copy (up to date): %s", dst)
                _last_copy_at[str(dst)] = time.monotonic()
                return dst

        copy_sqlite_database(src, dst, timeout=timeout)
        _last_copy_at[str(dst)] = time.monotonic()
        return dst
