"""Config parsing — ``image_dirs`` env forms and the ``.secrets`` credential file."""
import json
import os

import pytest

from pka.config import Settings, _parse_path_list, parse_secrets_file, reject_system_path


class TestParsePathList:
    def test_empty_inputs_yield_empty_list(self):
        assert _parse_path_list(None) == []
        assert _parse_path_list("") == []
        assert _parse_path_list([]) == []

    def test_json_array_string(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        parsed = _parse_path_list(json.dumps([str(a), str(b)]))
        assert parsed == [reject_system_path(a), reject_system_path(b)]

    def test_os_pathsep_string(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        parsed = _parse_path_list(f"{a}{os.pathsep}{b}")
        assert parsed == [reject_system_path(a), reject_system_path(b)]

    def test_bare_single_path(self, tmp_path):
        a = tmp_path / "solo"
        assert _parse_path_list(str(a)) == [reject_system_path(a)]

    def test_dedups_preserving_order(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        parsed = _parse_path_list([a, b, a])
        assert parsed == [reject_system_path(a), reject_system_path(b)]


class TestImageDirsSetting:
    def test_default_is_single_pictures_folder(self, monkeypatch):
        monkeypatch.delenv("ALEXANDRIA_IMAGE_DIRS", raising=False)
        monkeypatch.delenv("ALEXANDRIA_IMAGES_DIR", raising=False)
        s = Settings(_env_file=None)
        assert len(s.image_dirs) == 1
        assert s.image_dirs[0].name == "research"

    def test_env_json_array(self, monkeypatch, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        monkeypatch.setenv("ALEXANDRIA_IMAGE_DIRS", json.dumps([str(a), str(b)]))
        s = Settings(_env_file=None)
        assert s.image_dirs == [reject_system_path(a), reject_system_path(b)]

    def test_legacy_singular_env_var(self, monkeypatch, tmp_path):
        monkeypatch.delenv("ALEXANDRIA_IMAGE_DIRS", raising=False)
        legacy = tmp_path / "legacy"
        monkeypatch.setenv("ALEXANDRIA_IMAGES_DIR", str(legacy))
        s = Settings(_env_file=None)
        assert s.image_dirs == [reject_system_path(legacy)]


@pytest.fixture
def secrets_file(tmp_path, monkeypatch):
    """Point ALEXANDRIA_SECRETS_FILE at a tmp file; returns a writer callable."""
    path = tmp_path / ".secrets"
    monkeypatch.setenv("ALEXANDRIA_SECRETS_FILE", str(path))

    def write(text: str):
        path.write_text(text, encoding="utf-8")
        return path

    return write


class TestParseSecretsFile:
    def test_strips_prefix_comments_and_quotes(self, tmp_path):
        path = tmp_path / ".secrets"
        path.write_text(
            "# a comment\n"
            "\n"
            "SECRET_ALEXANDRIA_OPENROUTER_API_KEY=sk-or-plain\n"
            "  SECRET_ALEXANDRIA_REDDIT_FEED_URL = 'quoted value' \n"
            'export SECRET_ALEXANDRIA_OVH_API_KEY="dq"\n',
            encoding="utf-8",
        )
        assert parse_secrets_file(path) == {
            "ALEXANDRIA_OPENROUTER_API_KEY": "sk-or-plain",
            "ALEXANDRIA_REDDIT_FEED_URL": "quoted value",
            "ALEXANDRIA_OVH_API_KEY": "dq",
        }

    def test_ignores_unprefixed_and_malformed_lines(self, tmp_path):
        path = tmp_path / ".secrets"
        path.write_text(
            "ALEXANDRIA_DATA_DIR=/somewhere\nnot-a-pair\nSECRET_ALEXANDRIA_OVH_API_KEY=k\n",
            encoding="utf-8",
        )
        assert parse_secrets_file(path) == {"ALEXANDRIA_OVH_API_KEY": "k"}

    def test_value_containing_equals_is_kept_whole(self, tmp_path):
        path = tmp_path / ".secrets"
        path.write_text("SECRET_ALEXANDRIA_OVH_API_KEY=a=b=c\n", encoding="utf-8")
        assert parse_secrets_file(path) == {"ALEXANDRIA_OVH_API_KEY": "a=b=c"}


class TestSecretsFileSource:
    def test_populates_the_matching_setting(self, secrets_file, monkeypatch):
        monkeypatch.delenv("ALEXANDRIA_OPENROUTER_API_KEY", raising=False)
        secrets_file("SECRET_ALEXANDRIA_OPENROUTER_API_KEY=sk-or-secret\n")
        assert Settings(_env_file=None).openrouter_api_key == "sk-or-secret"

    def test_env_var_wins_over_secrets_file(self, secrets_file, monkeypatch):
        secrets_file("SECRET_ALEXANDRIA_OPENROUTER_API_KEY=from-secrets\n")
        monkeypatch.setenv("ALEXANDRIA_OPENROUTER_API_KEY", "from-env")
        assert Settings(_env_file=None).openrouter_api_key == "from-env"

    def test_secrets_file_wins_over_dotenv(self, secrets_file, monkeypatch, tmp_path):
        monkeypatch.delenv("ALEXANDRIA_OPENROUTER_API_KEY", raising=False)
        secrets_file("SECRET_ALEXANDRIA_OPENROUTER_API_KEY=from-secrets\n")
        dotenv = tmp_path / "dotenv"
        dotenv.write_text("ALEXANDRIA_OPENROUTER_API_KEY=from-dotenv\n", encoding="utf-8")
        assert Settings(_env_file=dotenv).openrouter_api_key == "from-secrets"

    def test_unknown_setting_is_ignored(self, secrets_file, monkeypatch):
        monkeypatch.delenv("ALEXANDRIA_OPENROUTER_API_KEY", raising=False)
        secrets_file(
            "SECRET_ALEXANDRIA_NOT_A_SETTING=x\nSECRET_ALEXANDRIA_OPENROUTER_API_KEY=ok\n"
        )
        assert Settings(_env_file=None).openrouter_api_key == "ok"

    def test_missing_file_falls_back_to_defaults(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ALEXANDRIA_OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("ALEXANDRIA_SECRETS_FILE", str(tmp_path / "absent"))
        assert Settings(_env_file=None).openrouter_api_key == ""

    def test_empty_path_disables_the_source(self, tmp_path, monkeypatch):
        """The suite-wide isolation switch: ALEXANDRIA_SECRETS_FILE='' reads nothing."""
        monkeypatch.delenv("ALEXANDRIA_OPENROUTER_API_KEY", raising=False)
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".secrets").write_text(
            "SECRET_ALEXANDRIA_OPENROUTER_API_KEY=leaked\n", encoding="utf-8"
        )
        monkeypatch.setenv("ALEXANDRIA_SECRETS_FILE", "")
        assert Settings(_env_file=None).openrouter_api_key == ""
