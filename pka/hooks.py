"""Listeners for events in the shared ingest tail, registered from above it.

Ingestion and clustering sit below tag training in the import layering
(``[tool.importlinter]`` in ``pyproject.toml``), so the tail cannot call tag
training itself. It announces the event here instead, and whatever has to react
registers a listener at start-up, in :func:`pka.bootstrap.install_hooks`.

This module imports nothing from ``pka``, so every layer may call into it.
"""

import logging
from collections.abc import Callable

log = logging.getLogger(__name__)

DocumentListener = Callable[[int], None]

_document_embedded: list[DocumentListener] = []


def on_document_embedded(listener: DocumentListener) -> None:
    """Call ``listener(doc_id)`` each time a document's embedding is refreshed.

    Registering the same listener again is a no-op, so start-up wiring can run
    more than once in a process.
    """
    if listener not in _document_embedded:
        _document_embedded.append(listener)


def document_embedded(doc_id: int) -> None:
    """Run every listener for a freshly embedded document.

    A listener that raises is logged and skipped: the document is already
    embedded, and a failure in something reacting to that must not fail the
    ingest that triggered it.
    """
    for listener in list(_document_embedded):
        try:
            listener(doc_id)
        except Exception:
            log.exception(
                "document_embedded listener %s failed for document %d",
                getattr(listener, "__qualname__", listener),
                doc_id,
            )


def document_embedded_listeners() -> tuple[DocumentListener, ...]:
    """The registered listeners, in call order."""
    return tuple(_document_embedded)
