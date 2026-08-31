from pathlib import Path

import pytest
from PIL import Image as PILImage


def _make_image(path: Path, size=(200, 150), color="white") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    PILImage.new("RGB", size, color=color).save(path)
    return path


@pytest.fixture()
def image_folder(tmp_path) -> Path:
    root = tmp_path / "images"
    _make_image(root / "cover.jpg", (400, 600), "blue")
    _make_image(root / "slide.png", (1920, 1080), "white")
    _make_image(root / "sub" / "notes.jpg", (800, 600), "yellow")
    (root / "not_an_image.txt").parent.mkdir(parents=True, exist_ok=True)
    (root / "not_an_image.txt").write_text("ignore me")
    return root


class TestScanImages:
    def test_finds_correct_count(self, image_folder):
        from pka.connectors.images import scan_images

        imgs = scan_images(image_folder)
        assert len(imgs) == 3

    def test_ignores_non_images(self, image_folder):
        from pka.connectors.images import scan_images

        imgs = scan_images(image_folder)
        assert all(i.filename.endswith((".jpg", ".png")) for i in imgs)

    def test_recurses_into_subdirs(self, image_folder):
        from pka.connectors.images import scan_images

        imgs = scan_images(image_folder)
        filenames = {i.filename for i in imgs}
        assert "notes.jpg" in filenames

    def test_dimensions_extracted(self, image_folder):
        from pka.connectors.images import scan_images

        imgs = scan_images(image_folder)
        slide = next(i for i in imgs if i.filename == "slide.png")
        assert slide.width == 1920
        assert slide.height == 1080

    def test_file_size_positive(self, image_folder):
        from pka.connectors.images import scan_images

        imgs = scan_images(image_folder)
        assert all(i.file_size > 0 for i in imgs)

    def test_date_taken_is_int(self, image_folder):
        from pka.connectors.images import scan_images

        imgs = scan_images(image_folder)
        assert all(isinstance(i.date_taken, int) for i in imgs)

    def test_raises_if_folder_missing(self, tmp_path):
        from pka.connectors.images import scan_images

        with pytest.raises(FileNotFoundError):
            scan_images(tmp_path / "no_such_folder")

    def test_empty_folder_returns_empty_list(self, tmp_path):
        from pka.connectors.images import scan_images

        (tmp_path / "empty").mkdir()
        assert scan_images(tmp_path / "empty") == []


class TestScanImageDirs:
    def test_scans_multiple_folders(self, tmp_path):
        from pka.connectors.images import scan_image_dirs

        a = tmp_path / "a"
        b = tmp_path / "b"
        _make_image(a / "one.jpg")
        _make_image(b / "two.png")
        imgs = scan_image_dirs([a, b])
        assert {i.filename for i in imgs} == {"one.jpg", "two.png"}

    def test_skips_missing_folders(self, tmp_path):
        from pka.connectors.images import scan_image_dirs

        a = tmp_path / "a"
        _make_image(a / "one.jpg")
        imgs = scan_image_dirs([a, tmp_path / "gone"])
        assert {i.filename for i in imgs} == {"one.jpg"}

    def test_dedups_files_shared_between_nested_roots(self, tmp_path):
        from pka.connectors.images import scan_image_dirs

        root = tmp_path / "root"
        sub = root / "sub"
        _make_image(sub / "shared.jpg")
        # ``sub`` nested inside ``root`` → the file is reachable from both roots.
        imgs = scan_image_dirs([root, sub])
        assert [i.filename for i in imgs] == ["shared.jpg"]

    def test_empty_list_returns_empty(self, tmp_path):
        from pka.connectors.images import scan_image_dirs

        assert scan_image_dirs([]) == []
