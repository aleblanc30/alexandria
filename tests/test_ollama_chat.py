"""Tests for Ollama chat helpers (HTTP mocked)."""
import pytest

from pka import ollama_chat as oc


@pytest.fixture(autouse=True)
def _reset_chat_model_cache():
    oc._cached_chat_model = None
    yield
    oc._cached_chat_model = None


class TestIsChatModel:
    def test_rejects_embed_models(self):
        assert not oc._is_chat_model("nomic-embed-text")
        assert not oc._is_chat_model("mxbai-embed-large")
        assert oc._is_chat_model("llama3")


class TestResolveChatModel:
    def test_explicit_model(self):
        assert oc.resolve_chat_model("custom-model") == "custom-model"

    def test_configured_chat_model(self, monkeypatch):
        monkeypatch.setattr(oc.cfg, "chat_model", "configured")
        assert oc.resolve_chat_model() == "configured"

    def test_auto_detect_from_tags(self, monkeypatch):
        monkeypatch.setattr(oc.cfg, "chat_model", "")
        oc._cached_chat_model = None

        class FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"models": [{"name": "nomic-embed-text"}, {"name": "llama3:8b"}]}

        monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResp())
        assert oc.resolve_chat_model() == "llama3:8b"

    def test_fallback_when_tags_fail(self, monkeypatch):
        monkeypatch.setattr(oc.cfg, "chat_model", "")
        oc._cached_chat_model = None
        monkeypatch.setattr("httpx.get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
        assert oc.resolve_chat_model() in ("llama3", "")


class TestChatJson:
    def test_parses_valid_json(self, monkeypatch):
        monkeypatch.setattr(oc, "resolve_chat_model", lambda m=None: "llama3")

        class FakeResp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"message": {"content": '{"label": "AI"}'}}

        monkeypatch.setattr("httpx.post", lambda *a, **k: FakeResp())
        data, err = oc.chat_json("prompt")
        assert err is None
        assert data["label"] == "AI"

    def test_empty_content_returns_error(self, monkeypatch):
        monkeypatch.setattr(oc, "resolve_chat_model", lambda m=None: "llama3")

        class FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"message": {"content": "   "}}

        monkeypatch.setattr("httpx.post", lambda *a, **k: FakeResp())
        data, err = oc.chat_json("prompt")
        assert data == {}
        assert err is not None

    def test_http_failure_returns_error(self, monkeypatch):
        monkeypatch.setattr(oc, "resolve_chat_model", lambda m=None: "llama3")
        monkeypatch.setattr(
            "httpx.post",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("connection refused")),
        )
        data, err = oc.chat_json("prompt")
        assert data == {}
        assert "connection refused" in err
