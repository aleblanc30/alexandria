"""The process-wide SQLAlchemy engine for ``archive.db``.

Every helper in :mod:`pka.db` looks ``get_engine`` up on this module at call
time (``engine.get_engine()``) rather than importing the function, so a test
fixture that replaces it here is seen by all of them.
"""

import sqlalchemy as sa

from pka.config import settings as cfg

_engine: sa.Engine | None = None


def get_engine() -> sa.Engine:
    global _engine
    if _engine is None:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = sa.create_engine(
            f"sqlite:///{cfg.archive_db}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        with _engine.connect() as con:
            con.execute(sa.text("PRAGMA journal_mode=WAL"))
            con.execute(sa.text("PRAGMA synchronous=NORMAL"))
            con.commit()
    return _engine
