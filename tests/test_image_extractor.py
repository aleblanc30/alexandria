"""Unit tests for image extraction helpers (no real Ollama/CLIP/OCR)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image as PILImage


@pytest.fixture()
def sample_png(tmp_path) -> Path:
    p = tmp_path / "photo.png"
    PILImage.new("RGB", (800, 600), color="red").save(p)
    return p


class TestParseLlmJson:
    def test_parses_plain_json(self):
        from pka.ingestion.image_extractor import _parse_llm_json

        raw = '{"image_type": "slide", "description": "A slide."}'
        assert _parse_llm_json(raw)["image_type"] == "slide"

    def test_strips_markdown_fences(self):
        from pka.ingestion.image_extractor import _parse_llm_json

        raw = '```json\n{"image_type": "poster", "description": "Poster."}\n```'
        assert _parse_llm_json(raw)["image_type"] == "poster"

    def test_extracts_embedded_object(self):
        from pka.ingestion.image_extractor import _parse_llm_json

        raw = 'Here is the result: {"image_type": "notes", "description": "Notes."}'
        assert _parse_llm_json(raw)["image_type"] == "notes"


class TestEncodeImage:
    def test_returns_base64_string(self, sample_png):
        from pka.ingestion.image_extractor import _encode_image

        b64 = _encode_image(sample_png)
        assert isinstance(b64, str)
        assert len(b64) > 20

    def test_downscales_large_images(self, tmp_path):
        from pka.ingestion.image_extractor import _encode_image

        p = tmp_path / "big.png"
        PILImage.new("RGB", (3000, 2000), color="blue").save(p)
        b64 = _encode_image(p, max_px=512)
        assert len(b64) > 0


class TestClassifyAndDescribe:
    def test_success(self, sample_png, monkeypatch):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "message": {"content": '{"image_type": "slide", "description": "ML slide."}'},
        }
        monkeypatch.setattr("pka.providers.ollama.httpx.post", lambda *a, **kw: resp)

        from pka.ingestion.image_extractor import classify_and_describe

        itype, desc = classify_and_describe(sample_png)
        assert itype == "slide"
        assert "ML" in desc

    def test_invalid_type_becomes_unknown(self, sample_png, monkeypatch):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "message": {"content": '{"image_type": "not_valid", "description": "x"}'},
        }
        monkeypatch.setattr("pka.providers.ollama.httpx.post", lambda *a, **kw: resp)

        from pka.ingestion.image_extractor import classify_and_describe

        itype, _ = classify_and_describe(sample_png)
        assert itype == "unknown"

    def test_spaced_label_maps_to_enum(self, sample_png, monkeypatch):
        """Models answer "book cover", not "book_cover" — that is a hit, not unknown.

        Regression: the strict membership test discarded it, so the admission
        gate rejected (and permanently cached) correctly classified book covers.
        """
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "message": {"content": '{"image_type": "book cover", "description": "A cover."}'},
        }
        monkeypatch.setattr("pka.providers.ollama.httpx.post", lambda *a, **kw: resp)

        from pka.ingestion.image_extractor import classify_and_describe

        itype, desc = classify_and_describe(sample_png)
        assert itype == "book_cover"
        assert desc == "A cover."

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Book Cover", "book_cover"),
            ("book-cover", "book_cover"),
            ("  WHITEBOARD  ", "whiteboard"),
            ("multiple book covers", "multiple_book_covers"),
            ("book covers", "multiple_book_covers"),  # alias, not the single-cover label
            ("Bookshelves", "bookshelf"),
            ("bookcase", "bookshelf"),
            ("book cover shelf thing", "unknown"),  # not a label, must not fuzzy-match
            ("", "unknown"),
        ],
    )
    def test_label_normalisation(self, raw, expected):
        from pka.ingestion.image_extractor import _normalize_type

        assert _normalize_type(raw) == expected

    def test_salvage_maps_spaced_label(self):
        """The invalid-JSON salvage path must fold labels the same way."""
        from pka.ingestion.image_extractor import _salvage_vision_fields

        broken = '{"image_type": "book cover", "description": "A "signed" first edition."}'
        itype, desc = _salvage_vision_fields(broken)
        assert itype == "book_cover"
        assert desc == 'A "signed" first edition.'

    def test_failure_returns_unknown(self, sample_png, monkeypatch):
        monkeypatch.setattr(
            "pka.providers.ollama.httpx.post",
            lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("down")),
        )
        from pka.ingestion.image_extractor import classify_and_describe

        itype, desc = classify_and_describe(sample_png)
        assert itype == "unknown"
        assert desc == ""

    def test_salvages_unescaped_quotes_in_description(self, sample_png, monkeypatch):
        """Vision models emit unescaped quotes that break strict JSON; recover anyway."""
        broken = (
            '{\n  "image_type": "notes",\n'
            '  "description": "A photo of "handwritten" lecture notes."\n}'
        )
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"message": {"content": broken}}
        monkeypatch.setattr("pka.providers.ollama.httpx.post", lambda *a, **kw: resp)

        from pka.ingestion.image_extractor import classify_and_describe

        itype, desc = classify_and_describe(sample_png)
        assert itype == "notes"
        assert desc == 'A photo of "handwritten" lecture notes.'

    def test_requests_json_format(self, sample_png, monkeypatch):
        """The vision call must ask Ollama to grammar-constrain valid JSON."""
        captured = {}

        def _post(url, json=None, headers=None, timeout=None):
            captured["payload"] = json
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            resp.json.return_value = {
                "message": {"content": '{"image_type": "slide", "description": "x"}'},
            }
            return resp

        monkeypatch.setattr("pka.providers.ollama.httpx.post", _post)
        from pka.ingestion.image_extractor import classify_and_describe

        classify_and_describe(sample_png)
        assert captured["payload"]["format"] == "json"


class TestPerTypeContentPrompts:
    """The main vision pass prompts for *content*, chosen by the image category."""

    def _provider(self, reply: str):
        """A vision provider that records every prompt and returns ``reply``."""
        prov = MagicMock()
        prov.complete.return_value = reply
        return prov

    @pytest.mark.parametrize(
        ("image_type", "marker"),
        [
            ("slide", '"transcript"'),
            ("notes", '"transcript"'),
            ("whiteboard", '"transcript"'),
            ("poster", '"content"'),
            ("book_cover", '"books"'),
            ("multiple_book_covers", '"books"'),
            ("bookshelf", '"books"'),
        ],
    )
    def test_prompt_selected_by_type(self, sample_png, image_type, marker):
        from pka.ingestion.image_extractor import extract_image_content

        prov = self._provider('{"transcript": "x", "description": "y"}')
        extract_image_content(sample_png, image_type=image_type, provider=prov)
        prompt = prov.complete.call_args[0][0]
        assert marker in prompt
        assert prov.complete.call_count == 1  # gate label ⇒ no extra classify call

    def test_book_prompt_carries_label_hint(self, sample_png):
        """One book prompt covers all three labels, hinted by what is readable."""
        from pka.ingestion.image_extractor import extract_image_content

        prompts = {}
        for label in ("book_cover", "multiple_book_covers", "bookshelf"):
            prov = self._provider('{"books": [], "description": "d"}')
            extract_image_content(sample_png, image_type=label, provider=prov)
            prompts[label] = prov.complete.call_args[0][0]

        assert "exactly one entry" in prompts["book_cover"]
        assert "one entry per cover" in prompts["multiple_book_covers"]
        assert "SPINES" in prompts["bookshelf"]
        assert all('"books"' in p and "NEVER invent" in p for p in prompts.values())

    def test_gate_label_path_makes_one_call(self, sample_png):
        """Passing the gate's label costs exactly one vision call, and keeps it."""
        from pka.ingestion.image_extractor import extract_image_content

        prov = self._provider(
            '{"transcript": "sketch of the retrieval pipeline", "description": "A whiteboard."}'
        )
        out = extract_image_content(sample_png, image_type="whiteboard", provider=prov)
        assert prov.complete.call_count == 1
        assert out.image_type == "whiteboard"
        assert out.content == "sketch of the retrieval pipeline"
        assert out.description == "A whiteboard."

    def test_fallback_classifies_then_prompts(self, sample_png):
        """No gate label (gate off / --skip-gate): classify first, then the content prompt."""
        from pka.ingestion.image_extractor import extract_image_content

        prov = MagicMock()
        prov.complete.side_effect = [
            '{"image_type": "notes", "description": "A notebook page."}',
            '{"transcript": "hypothesis: retrieval is the bottleneck", "description": ""}',
        ]
        out = extract_image_content(sample_png, provider=prov)
        assert prov.complete.call_count == 2
        first, second = (call[0][0] for call in prov.complete.call_args_list)
        assert '"image_type"' in first  # classify prompt
        assert '"transcript"' in second  # per-type content prompt
        assert out.image_type == "notes"
        assert out.content == "hypothesis: retrieval is the bottleneck"
        assert out.description == "A notebook page."  # kept from the classify call

    def test_unknown_keeps_generic_behaviour(self, sample_png):
        from pka.ingestion.image_extractor import extract_image_content

        prov = self._provider('{"image_type": "unknown", "description": "A blank wall."}')
        out = extract_image_content(sample_png, provider=prov)
        assert prov.complete.call_count == 1  # no content prompt exists for unknown
        assert out.image_type == "unknown"
        assert out.description == "A blank wall."
        assert out.content == ""
        assert out.books == []

    def test_content_pass_failure_keeps_the_label(self, sample_png):
        from pka.ingestion.image_extractor import extract_image_content

        prov = MagicMock()
        prov.complete.side_effect = RuntimeError("backend down")
        out = extract_image_content(sample_png, image_type="slide", provider=prov)
        assert out.image_type == "slide"
        assert out.content == ""
        assert out.books == []


class TestBookExtraction:
    def test_multiple_books_extracted(self, sample_png):
        from pka.ingestion.image_extractor import extract_image_content

        prov = MagicMock()
        prov.complete.return_value = """{
          "books": [
            {"title": "Godel, Escher, Bach", "authors": ["Douglas Hofstadter"],
             "isbn": "978-0-465-02656-2"},
            {"title": "The Society of Mind", "authors": ["Marvin Minsky"], "isbn": null}
          ],
          "description": "Two paperbacks on a desk."
        }"""
        out = extract_image_content(sample_png, image_type="multiple_book_covers", provider=prov)
        assert out.books == [
            {
                "title": "Godel, Escher, Bach",
                "authors": ["Douglas Hofstadter"],
                "isbn": "9780465026562",
            },
            {"title": "The Society of Mind", "authors": ["Marvin Minsky"], "isbn": None},
        ]
        # The same fields are what gets indexed, via image_search_text.
        assert "Godel, Escher, Bach — Douglas Hofstadter" in out.content
        assert "The Society of Mind" in out.content
        assert out.description == "Two paperbacks on a desk."

    def test_spine_entries_may_be_partial(self, sample_png):
        """A shelf photo yields titles without authors or ISBNs — that is valid."""
        from pka.ingestion.image_extractor import extract_image_content

        prov = MagicMock()
        prov.complete.return_value = (
            '{"books": [{"title": "Seeing Like a State", "authors": [], "isbn": null},'
            ' {"title": "Data Feminism"}],'
            ' "description": "A shelf of spines."}'
        )
        out = extract_image_content(sample_png, image_type="bookshelf", provider=prov)
        assert [b["title"] for b in out.books] == ["Seeing Like a State", "Data Feminism"]
        assert all(b["isbn"] is None and b["authors"] == [] for b in out.books)

    def test_unreadable_titles_and_bogus_isbns_are_dropped(self, sample_png):
        """A wrong title is worse than a missing one — it becomes a bogus lookup."""
        from pka.ingestion.image_extractor import extract_image_content

        prov = MagicMock()
        prov.complete.return_value = (
            '{"books": [{"title": "unknown", "authors": ["Unknown"], "isbn": "123"},'
            ' {"title": "Thinking in Systems", "authors": "Donella Meadows",'
            '  "isbn": "1234"}],'
            ' "description": "d"}'
        )
        out = extract_image_content(sample_png, image_type="bookshelf", provider=prov)
        assert out.books == [
            {"title": "Thinking in Systems", "authors": ["Donella Meadows"], "isbn": None},
        ]

    def test_salvages_books_from_malformed_json(self, sample_png):
        """Trailing prose breaks strict JSON; the entries are still recoverable."""
        from pka.ingestion.image_extractor import extract_image_content

        prov = MagicMock()
        prov.complete.return_value = (
            'Here is what I can read: {"books": ['
            '{"title": "The Timeless Way of Building", "authors": ["Christopher Alexander"],'
            ' "isbn": "0195024024"},'
            '{"title": "A Pattern Language", "authors": ["Christopher Alexander"]}'
            '], "description": "Two hardbacks."} — hope that helps!'
        )
        out = extract_image_content(sample_png, image_type="book_cover", provider=prov)
        assert [b["title"] for b in out.books] == [
            "The Timeless Way of Building",
            "A Pattern Language",
        ]
        assert out.books[0]["isbn"] == "0195024024"
        assert out.books[1]["authors"] == ["Christopher Alexander"]

    def test_salvages_transcript_with_unescaped_quotes(self, sample_png):
        from pka.ingestion.image_extractor import extract_image_content

        prov = MagicMock()
        prov.complete.return_value = (
            '{"transcript": "the "hard" part is retrieval", "description": "A whiteboard."}'
        )
        out = extract_image_content(sample_png, image_type="whiteboard", provider=prov)
        assert "retrieval" in out.content
        assert out.description == "A whiteboard."


class TestOcrImage:
    @pytest.fixture(autouse=True)
    def _use_easyocr(self, monkeypatch):
        """Pin the EasyOCR backend — these tests exercise that provider."""
        import pka.providers as providers

        monkeypatch.setattr(providers.cfg, "ocr_provider", "easyocr")
        providers.reset_providers()

    def test_success(self, sample_png, monkeypatch):
        from pka.providers.easy_ocr import EasyOcrProvider

        reader = MagicMock()
        reader.readtext.return_value = ["  Hello OCR  "]
        monkeypatch.setattr(EasyOcrProvider, "_reader", lambda self, langs: reader)
        from pka.ingestion.image_extractor import ocr_image

        assert ocr_image(sample_png) == "Hello OCR"

    def test_failure_returns_empty(self, sample_png, monkeypatch):
        from pka.providers.easy_ocr import EasyOcrProvider

        def _boom(self, langs):
            raise RuntimeError("ocr fail")

        monkeypatch.setattr(EasyOcrProvider, "_reader", _boom)
        from pka.ingestion.image_extractor import ocr_image

        assert ocr_image(sample_png) == ""


class TestClipEmbed:
    class _FakeTensor:
        def __init__(self, values):
            self._values = values

        def norm(self, **kw):
            return self

        def __truediv__(self, other):
            return self

        def squeeze(self):
            return self

        def tolist(self):
            return self._values

    def _mock_torch(self, monkeypatch):
        monkeypatch.setattr(
            "torch.no_grad",
            lambda: MagicMock(
                __enter__=MagicMock(return_value=None), __exit__=MagicMock(return_value=False)
            ),
        )

    def test_image_embedding(self, sample_png, monkeypatch):
        self._mock_torch(monkeypatch)
        fake_model = MagicMock()
        fake_model.get_image_features.return_value = self._FakeTensor([0.1, 0.2, 0.3])
        fake_processor = MagicMock(return_value={"pixel_values": "x"})

        monkeypatch.setattr(
            "pka.providers.clip._load_clip",
            lambda: (fake_model, fake_processor),
        )

        from pka.ingestion.image_extractor import clip_embed_image

        assert clip_embed_image(sample_png) == [0.1, 0.2, 0.3]

    def test_text_embedding(self, monkeypatch):
        self._mock_torch(monkeypatch)
        fake_model = MagicMock()
        fake_model.get_text_features.return_value = self._FakeTensor([0.5, 0.6])
        fake_processor = MagicMock(return_value={"input_ids": "x"})

        monkeypatch.setattr(
            "pka.providers.clip._load_clip",
            lambda: (fake_model, fake_processor),
        )

        from pka.ingestion.image_extractor import clip_embed_text

        assert clip_embed_text("query") == [0.5, 0.6]

    def test_clip_failure_returns_none(self, sample_png, monkeypatch):
        monkeypatch.setattr(
            "pka.providers.clip._load_clip",
            lambda: (_ for _ in ()).throw(RuntimeError("clip")),
        )
        from pka.ingestion.image_extractor import clip_embed_image

        assert clip_embed_image(sample_png) is None


class TestImageSearchText:
    def test_combines_description_and_ocr(self):
        from pka.ingestion.image_extractor import image_search_text

        text = image_search_text("OCR text", "Description here")
        assert text == "Description here\n\nOCR text"

    def test_empty_input_returns_none(self):
        from pka.ingestion.image_extractor import image_search_text

        assert image_search_text("", "") is None

    def test_ocr_only(self):
        from pka.ingestion.image_extractor import image_search_text

        assert image_search_text("scan line", "") == "scan line"
