"""Text embedding models for the chunk index.

The archive embeds every chunk and every search query with one model, and the
vectors are only comparable within it. :func:`get_embedder` returns that model
by name; :mod:`pka.storage.vector_store` decides which name is in force (the one
the collection was built with, see there).

Two kinds of model:

- ``all-MiniLM-L6-v2``, the model every archive was built with before the model
  became a setting. It runs through Chroma's own ONNX ``DefaultEmbeddingFunction``
  so its vectors stay identical to the ones already stored.
- Anything else, through ``sentence-transformers``, which applies the pooling
  and normalisation the model declares. Models trained with instruction
  prefixes (the E5 family: ``query: `` / ``passage: ``) get them from
  :data:`_PREFIXES`; without them E5 retrieval degrades.

A model is downloaded once from the Hugging Face Hub when it is not cached,
then loaded offline, the same rule as the CLIP model. Hub telemetry is
switched off (DESIGN.md §1.1).
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Protocol

log = logging.getLogger(__name__)

LEGACY_MODEL = "all-MiniLM-L6-v2"

# (query prefix, passage prefix) per model that was trained with them.
_PREFIXES: dict[str, tuple[str, str]] = {
    "intfloat/multilingual-e5-small": ("query: ", "passage: "),
    "intfloat/multilingual-e5-base": ("query: ", "passage: "),
    "intfloat/multilingual-e5-large": ("query: ", "passage: "),
}


class Embedder(Protocol):
    name: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def _as_floats(vectors: Any) -> list[list[float]]:
    """Native Python floats, whatever the model returned.

    Chroma rejects numpy scalars, and ``list(vec)`` over a numpy row yields
    exactly those; ``tolist()`` converts in C where it exists.
    """
    return [vec.tolist() if hasattr(vec, "tolist") else [float(x) for x in vec] for vec in vectors]


class _ChromaDefault:
    """The pre-setting model, through the same ONNX function that built old archives."""

    name = LEGACY_MODEL

    def __init__(self) -> None:
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

        self._fn = DefaultEmbeddingFunction()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return _as_floats(self._fn(texts))

    def embed_query(self, text: str) -> list[float]:
        return _as_floats(self._fn([text]))[0]


class _SentenceTransformer:
    def __init__(self, name: str) -> None:
        self.name = name
        self._query_prefix, self._passage_prefix = _PREFIXES.get(name, ("", ""))
        self._model = _load_sentence_transformer(name)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(
            [self._passage_prefix + t for t in texts], normalize_embeddings=True
        )
        return _as_floats(vectors)

    def embed_query(self, text: str) -> list[float]:
        vectors = self._model.encode([self._query_prefix + text], normalize_embeddings=True)
        return _as_floats(vectors)[0]


def _load_sentence_transformer(name: str):
    # sentence-transformers pulls in torch: imported only when a model other
    # than the legacy one is actually used. The Hub reads both variables when
    # it is first imported, so they are set before it.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    from sentence_transformers import SentenceTransformer

    try:
        return SentenceTransformer(name)
    except OSError:
        log.info("Embedding model %s not cached — downloading once…", name)
        os.environ["HF_HUB_OFFLINE"] = "0"
        return SentenceTransformer(name)


_embedders: dict[str, Embedder] = {}
_lock = threading.Lock()


def get_embedder(name: str) -> Embedder:
    """The embedder for model *name*, loaded once per process."""
    with _lock:
        if name not in _embedders:
            _embedders[name] = (
                _ChromaDefault() if name == LEGACY_MODEL else _SentenceTransformer(name)
            )
        return _embedders[name]
