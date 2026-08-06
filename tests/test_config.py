"""Config parsing — the list-valued ``image_dirs`` accepts several env forms."""
import json
import os

from pka.config import Settings, _parse_path_list, reject_system_path


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
