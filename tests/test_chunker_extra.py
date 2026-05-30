"""Tests for text chunking helpers."""
from pka.ingestion.chunker import clean_text, sentence_window_chunks


class TestChunkerEdgeCases:
    def test_spacy_backend_when_available(self, monkeypatch):
        class FakeSpan:
            def __init__(self, text):
                self.text = text

        class FakeDoc:
            def __init__(self, text):
                self.sents = [FakeSpan(s.strip()) for s in text.split(".") if s.strip()]

        fake_nlp = lambda text: FakeDoc(text)
        monkeypatch.setattr("pka.ingestion.chunker._get_spacy", lambda: fake_nlp)

        text = "First sentence here. Second sentence follows. Third one too."
        chunks = sentence_window_chunks(text, window=2, overlap=1, min_chars=10)
        assert len(chunks) >= 1

    def test_clean_text_dehyphenates(self):
        assert clean_text("inter-\nnet") == "internet"
