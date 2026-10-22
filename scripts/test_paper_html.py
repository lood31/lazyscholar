"""Offline HTML transport, sanitizer, cache and annotation regressions."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.request
from http.client import HTTPMessage
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from html_fixture import html_fixture

import paper_html as reader

ID = "2510.16720"
URL = "https://arxiv.org/html/" + ID


class HtmlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lazyscholar-html-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.doc = reader.store_document(self.root, html_fixture(), ID, URL)

    def test_structure_and_chunk_anchors(self):
        self.assertEqual(self.doc["math_count"], 58)
        self.assertGreater(len(self.doc["chunks"]), 1)
        self.assertGreater(self.doc["anchors"]["ls-source-S2"], 1)
        text = "".join(c["html"] for c in self.doc["chunks"])
        for name in ("msup", "msub", "msubsup", "mfrac"):
            self.assertIn("<" + name, text)
        self.assertIn('data-block="p1-b0"', text)
        self.assertNotIn("application/javascript", text)

    def test_source_identity_and_cache_without_network(self):
        with patch.object(reader, "_download", side_effect=AssertionError("Unexpected download")):
            self.assertEqual(reader.get_page(self.root, ID, 1)["source"], self.doc["source"])
        other = reader.prepare_document(html_fixture().replace(b"selectable", b"updated"), ID, URL)
        self.assertNotEqual(other["source"], self.doc["source"])

    def test_download_empty_cache_and_failed_refresh_preserves_cache(self):
        root = self.root / "new"
        with patch.object(reader, "_download", return_value=(html_fixture(), URL, "text/html")) as fetch:
            self.assertEqual(reader.get_page(root, ID, 1)["math_count"], 58)
            self.assertEqual(fetch.call_count, 1)
        before = (root / "article.json").read_bytes()
        with patch.object(reader, "_download", return_value=(b"<h1>Error</h1>", URL, "text/html")), self.assertRaises(reader.HtmlUnavailable):
            reader.get_page(root, ID, 1, refresh=True)
        self.assertEqual((root / "article.json").read_bytes(), before)

    def test_sanitizer_removes_active_content_and_external_images(self):
        injection = b'''<script>alert(1)</script><style>body{display:none}</style>
<iframe src="https://evil.example"></iframe><form><input name="x"></form>
<p id="btn-hl" class="toolbar ltx_font_bold" onclick="alert(1)" style="display:none">Safe.</p>
<a href="javascript:alert(1)">bad link</a><a href="http://127.0.0.1:8080/">private link</a>
<img src="https://evil.example/tracker" onerror="alert(1)" srcset="https://evil.example/a 2x">
<svg><script>alert(1)</script></svg>'''
        document = reader.prepare_document(html_fixture().replace(b"</article>", injection + b"</article>"), ID, URL)
        html = "".join(c["html"] for c in document["chunks"])
        for fragment in ("<script", "<style", "<iframe", "<form", "<input", "<svg", "onclick=", "onerror=", "srcset=", "javascript:", 'id="btn-hl"', 'href="http://127.'):
            self.assertNotIn(fragment, html)
        self.assertIn('id="ls-source-btn-hl"', html)
        self.assertIn('class="ltx_font_bold"', html)

    def test_origin_and_redirect_boundaries(self):
        handler = reader._SameOrigin()
        request = urllib.request.Request(URL)
        stream, headers = io.BytesIO(), HTTPMessage()
        for target in ("http://arxiv.org/other", "https://evil.example/", "https://arxiv.org@127.0.0.1/a", "file:///etc/hosts"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                handler.redirect_request(request, stream, 302, "", headers, target)
        redirected = handler.redirect_request(request, stream, 302, "", headers, "/html/other")
        assert redirected is not None
        self.assertEqual(redirected.full_url, "https://arxiv.org/html/other")
        for proxy in ("socks://localhost:1234", "http://u:p@localhost:1234", "http://localhost:1234/path"):
            with self.assertRaises(ValueError):
                reader.validate_proxy(proxy)
        self.assertEqual(reader.validate_proxy(""), "")
        self.assertEqual(reader.validate_proxy("http://127.0.0.1:1234"), "http://127.0.0.1:1234")

    def test_download_size_bound(self):
        response = MagicMock()
        response.status = 200
        response.read.return_value = b"x" * 17
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch.object(reader.urllib.request, "build_opener", return_value=opener), self.assertRaises(reader.HtmlUnavailable):
            reader._download(URL, "", 16)
        response.read.assert_called_once_with(17)

    def test_annotations_utf16_source_guard_and_reopen(self):
        run = self.doc["runs"][0]
        quote = run["text"][:10]
        row = {"block": run["id"], "start": 0, "end": len(quote.encode("utf-16-le")) // 2, "quote": quote}
        body = {"source": self.doc["source"], "highlights": [row]}
        reader.save_highlights(self.root, self.doc, body)
        self.assertEqual(reader.load_highlights(self.root, self.doc)["highlights"], [row])
        with self.assertRaises(ValueError):
            reader.save_highlights(self.root, self.doc, {**body, "source": "stale"})
        with self.assertRaises(ValueError):
            reader.save_highlights(self.root, self.doc, {**body, "highlights": [{**row, "quote": "wrong"}]})

    def test_legacy_unique_match_and_unresolved_retained(self):
        old = self.root / "legacy.json"
        original = {"highlights": [{"block": "p1-b0", "start": 0, "end": 25, "quote": "A unique legacy selection"},
                                    {"block": "p2-b0", "start": 0, "end": 9, "quote": "not found"}]}
        old.write_text(json.dumps(original), encoding="utf-8")
        result = reader.load_highlights(self.root, self.doc, old)
        self.assertTrue(result["highlights"])
        self.assertEqual(result["legacy_unresolved"][0]["quote"], "not found")
        self.assertEqual(json.loads(old.read_text()), original)
        reader.save_highlights(self.root, self.doc, {"source": self.doc["source"], "highlights": []}, old)
        self.assertEqual(reader.load_highlights(self.root, self.doc)["legacy_unresolved"], result["legacy_unresolved"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
