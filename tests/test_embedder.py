"""
Embedder tests — Ollama HTTP calls are fully mocked.
We test batching logic, error handling, and the shape of outputs.
"""
import pytest
import httpx
from unittest.mock import patch, MagicMock

import pka.ingestion.embedder as emb

FAKE_VECTOR = [0.1, 0.2, 0.3, 0.4]


def _mock_response(vector: list[float]) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {"embedding": vector}
    resp.raise_for_status.return_value = None
    return resp


class TestEmbedOne:
    def test_returns_list_of_floats(self):
        with patch("httpx.post", return_value=_mock_response(FAKE_VECTOR)):
            result = emb.embed_one("hello world")
        assert result == FAKE_VECTOR

    def test_posts_to_correct_url(self):
        with patch("httpx.post", return_value=_mock_response(FAKE_VECTOR)) as mock_post:
            emb.embed_one("test")
        url = mock_post.call_args[0][0]
        assert "/api/embeddings" in url

    def test_sends_correct_model(self):
        with patch("httpx.post", return_value=_mock_response(FAKE_VECTOR)) as mock_post:
            emb.embed_one("test")
        payload = mock_post.call_args[1]["json"]
        assert payload["model"] == "nomic-embed-text"

    def test_sends_prompt_text(self):
        with patch("httpx.post", return_value=_mock_response(FAKE_VECTOR)) as mock_post:
            emb.embed_one("my input text")
        payload = mock_post.call_args[1]["json"]
        assert payload["prompt"] == "my input text"

    def test_raises_on_http_error(self):
        bad = MagicMock(spec=httpx.Response)
        bad.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=bad
        )
        with patch("httpx.post", return_value=bad):
            with pytest.raises(httpx.HTTPStatusError):
                emb.embed_one("fail")


class TestEmbedBatch:
    def test_returns_one_vector_per_text(self):
        texts = ["a", "b", "c"]
        with patch("httpx.post", return_value=_mock_response(FAKE_VECTOR)):
            results = emb.embed_batch(texts)
        assert len(results) == len(texts)

    def test_empty_input_returns_empty(self):
        with patch("httpx.post", return_value=_mock_response(FAKE_VECTOR)):
            assert emb.embed_batch([]) == []

    def test_batching_limits_calls(self):
        texts = [str(i) for i in range(10)]
        call_count = 0

        def counting_post(*a, **kw):
            nonlocal call_count
            call_count += 1
            return _mock_response(FAKE_VECTOR)

        with patch("httpx.post", side_effect=counting_post):
            emb.embed_batch(texts, batch_size=3)

        assert call_count == 10   # embed_one called per text within batches

    def test_all_vectors_same_dimension(self):
        texts = ["foo", "bar", "baz"]
        with patch("httpx.post", return_value=_mock_response(FAKE_VECTOR)):
            results = emb.embed_batch(texts)
        dims = {len(v) for v in results}
        assert len(dims) == 1
