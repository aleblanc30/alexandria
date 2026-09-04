"""Tests for the fetch-time interstitial gate.

The samples are taken from what the real archive had stored as document text,
so a regression here is a regression against pages that actually shipped junk.
"""

import pytest

from pka.ingestion.content_gate import interstitial_reason


class TestConsentAndScriptWalls:
    @pytest.mark.parametrize(
        "text",
        [
            "JavaScript is disabled in your browser. Please enable JavaScript to proceed.",
            "Note: Your browser does not support JavaScript, Press Continue to proceed...",
            "You need to enable JavaScript to run this app.",
            "JavaScript must be enabled. Outlook",
            "Bevor Sie zu YouTube weitergehen Wir verwenden Cookies und Daten",
            "Before you continue to Google We use cookies and data to deliver services",
            "Client Challenge #loading-error font-size 16px",
        ],
    )
    def test_recognized(self, text):
        assert interstitial_reason(text) == "interstitial: consent or script wall"


class TestExtractorResidue:
    def test_search_results_stylesheet(self):
        """What a bookmarked Google search-results page had stored as its text."""
        css = "a, a:link, a:visited, a:active, a:hover { color: #1a73e8; text-decoration: none; }"
        assert interstitial_reason(css) == "interstitial: stylesheet, not content"

    def test_at_rule(self):
        assert (
            interstitial_reason("@font-face{font-family:FabricMDL2Icons;src:url('//res.cdn')}")
            == "interstitial: stylesheet, not content"
        )

    def test_inline_script(self):
        js = '(adsbygoogle=window.adsbygoogle||[]).push({google_ad_client:"ca-pub-568"});'
        assert interstitial_reason(js) == "interstitial: inline script, not content"

    def test_amazon_page_timer_script(self):
        js = "var aPageStart = (new Date()).getTime(); var ue_t0=ue_t0||+new Date();"
        assert interstitial_reason(js) == "interstitial: inline script, not content"

    def test_json_ld(self):
        text = '{"@context":"https://schema.org","@type":"Book","name":"L\'alpinisme"}'
        assert interstitial_reason(text) == "interstitial: embedded data, not content"

    def test_json_ld_after_the_page_title(self):
        """The extractor emits the title first, so a few leading tokens are
        tolerated before the brace."""
        text = 'NeuroML\n\n{ "@context": "https://schema.org", "@type": "WebSite" }'
        assert interstitial_reason(text) == "interstitial: embedded data, not content"

    def test_page_config_dump(self):
        text = 'YouTube\n\nwindow.WIZ_global_data = {"AfY8Hf":true,"MUE6Ne":"youtube_web"}'
        assert interstitial_reason(text) == "interstitial: embedded data, not content"


class TestRealContentPasses:
    def test_ordinary_prose(self):
        assert interstitial_reason("A history of the browser wars, from Netscape onward.") is None

    def test_empty_and_none(self):
        assert interstitial_reason(None) is None
        assert interstitial_reason("") is None
        assert interstitial_reason("   ") is None

    def test_an_article_about_consent_banners(self):
        """Only the opening is judged: a page *about* cookies discusses them in
        prose, an interstitial leads with them."""
        prose = "An essay on tracking. " * 12 + "we use cookies and data"
        assert len(prose) > 200
        assert interstitial_reason(prose) is None

    def test_a_page_whose_title_precedes_a_stylesheet(self):
        """Measured on the real archive: rejecting these took out pages that do
        have a title and only then run into extractor residue. The gate drops
        documents, so it errs towards keeping one."""
        text = 'Deep Learning\n\nDeep Learning html{ background-image: url("udem.jpg"); }'
        assert interstitial_reason(text) is None

    def test_long_text_does_not_backtrack(self):
        """Regression: an earlier pattern nested overlapping optional groups in
        a repetition and hung on real page text, which is where this runs."""
        import time

        start = time.perf_counter()
        assert interstitial_reason("word " * 20_000) is None
        assert time.perf_counter() - start < 1.0

    def test_an_article_that_quotes_css(self):
        """A CSS tutorial is real content — the rule must open the text, not
        merely appear in it."""
        text = (
            "Styling links is the first thing most people learn. The rule below "
            "sets every state at once: a:link, a:visited { color: #1a73e8; }"
        )
        assert interstitial_reason(text) is None
