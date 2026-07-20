"""Tests for LLM JSON response parsing."""
import json

import pytest

from pka.json_utils import parse_llm_json


def test_plain_json():
    assert parse_llm_json('{"label": "Topic"}') == {"label": "Topic"}


def test_fenced_json():
    raw = '```json\n{"label": "Topic", "description": "d"}\n```'
    assert parse_llm_json(raw) == {"label": "Topic", "description": "d"}


def test_fence_without_language_tag():
    assert parse_llm_json('```\n{"a": 1}\n```') == {"a": 1}


def test_json_embedded_in_prose():
    raw = 'Sure! Here is the result: {"label": "Raft"} Hope that helps.'
    assert parse_llm_json(raw) == {"label": "Raft"}


def test_garbage_raises():
    with pytest.raises(json.JSONDecodeError):
        parse_llm_json("no json here at all")
