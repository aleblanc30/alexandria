"""Move the archive to the configured embedding model.

Rebuilds the chunk index from SQLite chunk text with ``embedding_model``, then
recomputes document vectors and retrains the learned-tag models. Nothing is
fetched or re-chunked. Stop the server first: it would keep embedding with the
old model while the index is rebuilt.

Usage::

    alexandria reembed
"""

from __future__ import annotations

import argparse
import logging

from pka.cli._logging import setup_logging

log = logging.getLogger("reembed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="alexandria reembed",
        description="Rebuild every vector with the configured embedding model.",
    )
    parser.parse_args(argv)

    setup_logging()

    from pka.config import settings as cfg
    from pka.db.migrate import init_db
    from pka.reembed import reembed

    init_db()
    log.info("Re-embedding the archive with %s", cfg.embedding_model)
    stats = reembed()
    log.info(
        "Re-embedded %d chunk(s) and %d document(s) with %s; %d tag model(s) retrained",
        stats["processed"],
        stats["documents"],
        stats["embedding_model"],
        stats["tag_models_retrained"],
    )
    if stats["tag_models_dropped"]:
        log.warning(
            "Could not retrain, model removed: %s. Resume those sessions to label more.",
            ", ".join(stats["tag_models_dropped"]),
        )
    log.info("Re-run clustering: the accepted run was built from the old vectors.")
    return 0
