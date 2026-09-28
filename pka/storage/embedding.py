"""Text embedding models for the chunk index.

The archive embeds every chunk and every search query with one model, and the
vectors are only comparable within it. :func:`get_embedder` returns that model
by name; :mod:`pka.storage.vector_store` decides which name is in force (the one
the collection was built with, see there).

Two kinds of model:

- ``all-MiniLM-L6-v2``, the model every archive was built with before the model
  became a setting. It runs through Chroma's own ONNX model, the one behind
  ``DefaultEmbeddingFunction``, so its vectors stay identical to the ones
  already stored.
- Anything else, through ``sentence-transformers``, which applies the pooling
  and normalisation the model declares. Models trained with instruction
  prefixes (the E5 family: ``query: `` / ``passage: ``) get them from
  :data:`_PREFIXES`; without them E5 retrieval degrades.

Each embedder also hands the chunker its tokenizer and the most tokens a chunk
may hold, so a chunk is never longer than the model reads
(:mod:`pka.ingestion.chunker`).

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
    #: Counts tokens the way the model does; no padding or truncation.
    tokenizer: Any
    #: The most tokens of chunk text the model embeds without truncating it,
    #: after the passage prefix and special tokens.
    max_chunk_tokens: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def _counting_copy(tokenizer: Any) -> Any:
    """An independent copy of a ``tokenizers.Tokenizer`` with no padding or truncation.

    A model's own tokenizer pads or truncates to its input length, which would
    make every count the same; the copy leaves the model's untouched.
    """
    from tokenizers import Tokenizer

    copy = Tokenizer.from_str(tokenizer.to_str())
    copy.no_padding()
    copy.no_truncation()
    return copy


def _chunk_budget(tokenizer: Any, max_seq_length: int, passage_prefix: str) -> int:
    """*max_seq_length* less the prefix and the special tokens it is wrapped in."""
    return max_seq_length - len(tokenizer.encode(passage_prefix, add_special_tokens=True).ids)


def _as_floats(vectors: Any) -> list[list[float]]:
    """Native Python floats, whatever the model returned.

    Chroma rejects numpy scalars, and ``list(vec)`` over a numpy row yields
    exactly those; ``tolist()`` converts in C where it exists.
    """
    return [vec.tolist() if hasattr(vec, "tolist") else [float(x) for x in vec] for vec in vectors]


class _ChromaDefault:
    """The pre-setting model, through the same ONNX function that built old archives."""

    name = LEGACY_MODEL
    # sentence-transformers' setting for this model, which Chroma truncates at.
    _MAX_SEQ_LENGTH = 256

    def __init__(self) -> None:
        # The ONNX model itself: DefaultEmbeddingFunction builds a new one, and
        # reloads its weights, on every call.
        from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

        self._fn = ONNXMiniLM_L6_V2()
        # Its tokenizer is read from the model archive, which is otherwise
        # fetched only on the first embed.
        self._fn._download_model_if_not_exists()
        self.tokenizer = _counting_copy(self._fn.tokenizer)
        self.max_chunk_tokens = _chunk_budget(self.tokenizer, self._MAX_SEQ_LENGTH, "")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return _as_floats(self._fn(texts))

    def embed_query(self, text: str) -> list[float]:
        return _as_floats(self._fn([text]))[0]


class _SentenceTransformer:
    def __init__(self, name: str) -> None:
        self.name = name
        self._query_prefix, self._passage_prefix = _PREFIXES.get(name, ("", ""))
        self._model = _load_sentence_transformer(name)
        self.tokenizer = _counting_copy(self._model.tokenizer.backend_tokenizer)
        self.max_chunk_tokens = _chunk_budget(
            self.tokenizer, self._model.max_seq_length, self._passage_prefix
        )

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
