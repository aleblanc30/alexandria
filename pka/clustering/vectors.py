"""Shared vector math for clustering (cosine helpers)."""
from __future__ import annotations

import numpy as np


def l2_normalize_rows(matrix: np.ndarray) -> np.ndarray:
    """Row-wise L2 normalization with an epsilon guard for zero-norm rows."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1e-9, norms)
    return matrix / norms
