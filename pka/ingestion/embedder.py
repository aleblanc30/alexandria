"""
Ollama embedder wrapper with batching.

Uses the model configured at :data:`pka.config.settings.embed_model`
(default: ``nomic-embed-text``) reached at
:data:`pka.config.settings.ollama_base_url`.
"""
import logging

import httpx

from pka.config import settings as cfg

log = logging.getLogger(__name__)


def _embed_url() -> str:
    """Resolved lazily so test fixtures can override ``ollama_base_url``."""
    return f"{cfg.ollama_base_url}/api/embeddings"


def embed_one(text: str) -> list[float]:
    resp = httpx.post(
        _embed_url(),
        json={"model": cfg.embed_model, "prompt": text},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


def embed_batch(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    results: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        log.debug("Embedding batch %d–%d", i, i + len(batch))
        results.extend(embed_one(t) for t in batch)
    return results
