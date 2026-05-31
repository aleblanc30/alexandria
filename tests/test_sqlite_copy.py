import sqlite3

from pka.db.sqlite_copy import copy_sqlite_database


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
