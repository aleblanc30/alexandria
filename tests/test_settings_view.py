"""Read-only settings report: field grouping, secret redaction, capability resolution."""

import json

from pka.api import settings_view as sv
from pka.config import Settings


def _find_field(report: dict, name: str) -> dict:
    for group in report["groups"]:
        for field in group["fields"]:
            if field["name"] == name:
                return field
    raise AssertionError(f"field {name!r} not found in any group")


class TestGroups:
    def test_every_field_appears_exactly_once(self):
        all_names = [name for names in sv.GROUPS.values() for name in names]
        assert len(all_names) == len(set(all_names)), "a field appears in more than one group"
        assert set(all_names) == set(Settings.model_fields)


class TestBuildSettingsReport:
    def test_secret_field_redacted_when_set(self):
        from pka.config import settings

        settings.openrouter_api_key = "sk-super-secret-value"
        report = sv.build_settings_report()

        assert "sk-super-secret-value" not in json.dumps(report)
        field = _find_field(report, "openrouter_api_key")
        assert field["value"] is None
        assert field["is_secret"] is True
        assert field["is_set"] is True

    def test_secret_field_not_set(self):
        report = sv.build_settings_report()
        field = _find_field(report, "search_api_key")
        assert field["is_secret"] is True
        assert field["is_set"] is False
        assert field["value"] is None

    def test_reddit_feed_url_is_secret_by_explicit_name(self):
        # Doesn't match the *_api_key suffix rule — must be listed explicitly.
        assert sv.is_secret_field("reddit_feed_url") is True

    def test_is_default_true_for_stock_settings(self):
        report = sv.build_settings_report()
        field = _find_field(report, "chunk_sentences")
        assert field["is_default"] is True

    def test_is_default_false_for_override(self):
        from pka.config import settings

        settings.chunk_sentences = 99
        report = sv.build_settings_report()
        field = _find_field(report, "chunk_sentences")
        assert field["is_default"] is False
        assert field["value"] == 99

    def test_path_field_serializes_as_string(self):
        report = sv.build_settings_report()
        field = _find_field(report, "data_dir")
        assert isinstance(field["value"], str)

    def test_list_path_field_serializes_as_string_list(self):
        report = sv.build_settings_report()
        field = _find_field(report, "image_dirs")
        assert isinstance(field["value"], list)
        assert all(isinstance(v, str) for v in field["value"])

    def test_tier_is_present_for_every_field(self):
        report = sv.build_settings_report()
        for group in report["groups"]:
            for field in group["fields"]:
                assert field["tier"] in ("install_time", "operational", "tuning")


class TestCapabilityReport:
    def test_does_not_populate_provider_cache(self, monkeypatch):
        from pka.config import settings

        # Non-empty so chat resolution never probes localhost:11434 in this test.
        monkeypatch.setattr(settings, "chat_model", "llama3")

        import pka.providers as providers

        providers.reset_providers()
        sv.build_capability_report()
        assert providers._chat is None
        assert providers._vision is None
        assert providers._gate_vision is None
        assert providers._ocr is None
        assert providers._embedder is None

    def test_chat_capability_reflects_config(self, monkeypatch):
        from pka.config import settings

        monkeypatch.setattr(settings, "chat_provider", "openrouter")
        monkeypatch.setattr(settings, "openrouter_chat_model", "openai/gpt-4o-mini")
        monkeypatch.setattr(settings, "openrouter_api_key", "sk-test")

        report = sv.build_capability_report()
        chat = next(c for c in report if c["capability"] == "chat")
        assert chat["provider"] == "openrouter"
        assert chat["model"] == "openai/gpt-4o-mini"
        assert chat["credential_present"] is True

    def test_easyocr_reports_local_no_network(self, monkeypatch):
        from pka.config import settings

        monkeypatch.setattr(settings, "ocr_provider", "easyocr")
        report = sv.build_capability_report()
        ocr = next(c for c in report if c["capability"] == "ocr")
        assert ocr["provider"] == "easyocr"
        assert ocr["base_url"] == ""


class TestProbeProvider:
    def test_unreachable_returns_false_with_detail(self, monkeypatch):
        monkeypatch.setattr(
            "httpx.get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("connection refused"))
        )
        result = sv.probe_provider("chat")
        assert result["reachable"] is False
        assert "connection refused" in result["detail"]

    def test_reachable_returns_true(self, monkeypatch):
        class FakeResp:
            def raise_for_status(self):
                pass

        monkeypatch.setattr("httpx.get", lambda *a, **k: FakeResp())
        result = sv.probe_provider("chat")
        assert result["reachable"] is True

    def test_local_capability_skips_network(self, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("should not make an HTTP call")

        monkeypatch.setattr("httpx.get", _boom)
        result = sv.probe_provider("image_embed")
        assert result["reachable"] is True
        assert "no network" in result["detail"].lower()

    def test_unknown_capability_raises(self):
        try:
            sv.probe_provider("nope")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
