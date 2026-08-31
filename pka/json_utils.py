"""Shared helpers for parsing LLM JSON responses."""

from __future__ import annotations

import json
import re

_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def parse_llm_json(raw: str) -> dict:
    """Strip Markdown code fences and parse JSON.

    Falls back to extracting the first ``{...}`` block if direct parse fails.
    """
    cleaned = _JSON_FENCE_RE.sub("", raw).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[^{}]*\}", cleaned, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise
