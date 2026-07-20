import sqlite3

import pytest

from pka.db.sqlite_copy import copy_sqlite_database, ensure_sqlite_copy


def test_copy_sqlite_database_wal_mode(tmp_path):
    src = tmp_path / "live.sqlite"
    with sqlite3.connect(src) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t (x INTEGER)")
        con.execute("INSERT INTO t VALUES (1)")
        con.commit()
        con.execute("INSERT INTO t VALUES (2)")
        con.commit()

    dst = tmp_path / "snapshot.sqlite"
    copy_sqlite_database(src, dst)

    with sqlite3.connect(dst) as con:
        assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    assert not dst.with_name(f"{dst.name}-wal").exists()


def test_copy_sqlite_database_delete_journal_mode(tmp_path):
    src = tmp_path / "delete.sqlite"
    with sqlite3.connect(src) as con:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("CREATE TABLE t (x TEXT)")
        con.execute("INSERT INTO t VALUES ('a')")
        con.commit()

    dst = tmp_path / "copy.sqlite"
    copy_sqlite_database(src, dst)
    with sqlite3.connect(dst) as con:
        assert con.execute("SELECT x FROM t").fetchone()[0] == "a"


def test_copy_missing_source_raises(tmp_path):
    with pytest.raises(sqlite3.Error):
        copy_sqlite_database(tmp_path / "missing.sqlite", tmp_path / "out.sqlite")


def test_ensure_sqlite_copy_reuses_recent_snapshot(tmp_path, monkeypatch):
    src = tmp_path / "src.sqlite"
    dst = tmp_path / "dst.sqlite"
    with sqlite3.connect(src) as con:
        con.execute("CREATE TABLE t (x INTEGER)")
        con.execute("INSERT INTO t VALUES (1)")
        con.commit()

    copy_sqlite_database(src, dst)
    calls = {"n": 0}
    real_copy = copy_sqlite_database

    def counting_copy(*args, **kwargs):
        calls["n"] += 1
        return real_copy(*args, **kwargs)

    monkeypatch.setattr("pka.db.sqlite_copy.copy_sqlite_database", counting_copy)
    ensure_sqlite_copy(src, dst, min_interval_seconds=3600.0)
    assert calls["n"] == 0


def test_ensure_sqlite_copy_refreshes_when_source_newer(tmp_path):
    src = tmp_path / "src.sqlite"
    dst = tmp_path / "dst.sqlite"
    with sqlite3.connect(src) as con:
        con.execute("CREATE TABLE t (x INTEGER)")
        con.execute("INSERT INTO t VALUES (1)")
        con.commit()

    copy_sqlite_database(src, dst)
    with sqlite3.connect(src) as con:
        con.execute("INSERT INTO t VALUES (2)")
        con.commit()

    ensure_sqlite_copy(src, dst, min_interval_seconds=0.0, refresh=False)
    with sqlite3.connect(dst) as con:
        assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
