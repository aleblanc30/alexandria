"""Start-up wiring shared by the entry points.

:func:`install_hooks` runs in the API (``pka/api/main.py``) and in every CLI
command (``pka/cli/__init__.py``, which the ``scripts/run_*.py`` shims import
too). A new entry point that runs ingestion must call it, or documents ingested
there get no learned tags.
"""

from pka import hooks


def _apply_learned_tags(doc_id: int) -> None:
    # Imported on call, not at start-up: scoring pulls in numpy and the
    # tag-training engine, which ``alexandria --help`` has no use for.
    from pka.tag_training.scoring import apply_learned_tags_for_document

    apply_learned_tags_for_document(doc_id)


def install_hooks() -> None:
    """Register the listeners the ingest tail calls. Safe to call more than once."""
    hooks.on_document_embedded(_apply_learned_tags)
