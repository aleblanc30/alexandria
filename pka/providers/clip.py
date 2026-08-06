"""Local cross-modal embeddings via a CLIP model (``transformers``)."""

from __future__ import annotations

import logging
from pathlib import Path

from pka.config import settings as cfg

log = logging.getLogger(__name__)

_clip_model = None
_clip_processor = None


def _load_clip():
    global _clip_model, _clip_processor
    if _clip_model is None:
        import os

        # Load the cached model without a HF Hub round-trip. Even with weights
        # cached, from_pretrained otherwise re-checks the revision online on
        # every load — adding latency, emitting the "unauthenticated request"
        # warning, and failing when offline. HF_HUB_OFFLINE (set before the
        # first transformers/huggingface_hub import) forces a pure local load,
        # matching Alexandria's local-first design. (The kwarg local_files_only
        # alone does not suppress the round-trip in current huggingface_hub.)
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        from transformers import CLIPModel, CLIPProcessor

        model_name = cfg.clip_model

        def _load():
            proc = CLIPProcessor.from_pretrained(model_name)
            mdl = CLIPModel.from_pretrained(model_name)
            return proc, mdl

        try:
            log.info("Loading CLIP model %s from cache…", model_name)
            _clip_processor, _clip_model = _load()
        except OSError:
            # Not cached yet — allow a one-time download for this process.
            log.info("CLIP model %s not cached — downloading once…", model_name)
            os.environ["HF_HUB_OFFLINE"] = "0"
            os.environ["TRANSFORMERS_OFFLINE"] = "0"
            _clip_processor, _clip_model = _load()
        _clip_model.eval()
        log.info("CLIP model loaded.")
    return _clip_model, _clip_processor


def _pooled(features):
    """Unwrap ``get_image_features``/``get_text_features`` return value.

    transformers >= 4.56 wraps these in ``BaseModelOutputWithPooling`` (the
    projected embedding lives at ``.pooler_output``) instead of returning a
    plain tensor directly.
    """
    return features if hasattr(features, "norm") else features.pooler_output


class ClipImageEmbedder:
    """Normalised CLIP image/text embeddings in a shared vector space."""

    def embed_image(self, path: Path) -> list[float] | None:
        try:
            import torch
            from PIL import Image

            model, processor = _load_clip()
            with Image.open(path) as img:
                img = img.convert("RGB")
                inputs = processor(images=img, return_tensors="pt")

            with torch.no_grad():
                features = _pooled(model.get_image_features(**inputs))
                features = features / features.norm(dim=-1, keepdim=True)

            return features.squeeze().tolist()

        except Exception as exc:
            log.warning("CLIP embedding failed for %s: %s", path.name, exc)
            return None

    def embed_text(self, query: str) -> list[float] | None:
        try:
            import torch

            model, processor = _load_clip()
            inputs = processor(text=[query], return_tensors="pt", padding=True)
            with torch.no_grad():
                features = _pooled(model.get_text_features(**inputs))
                features = features / features.norm(dim=-1, keepdim=True)
            return features.squeeze().tolist()
        except Exception as exc:
            log.warning("CLIP text embedding failed: %s", exc)
            return None
