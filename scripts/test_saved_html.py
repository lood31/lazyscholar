"""Offline saved-webpage import, resource isolation, SVG and API regressions."""
from __future__ import annotations

import io
import unittest
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from unittest.mock import patch

import test_app_backend as backend

app = backend.app
saved = app.saved_html
reader = app.paper_html


def fixture(image="figure.png"):
    return ("<!doctype html><html><head><title>Saved paper fixture</title></head><body><article>"
            "<h1>Offline saved article</h1><p>Faithful native mathematics "
            "<span class='katex'><span class='katex-mathml'><math><mfrac><mi>a</mi><mi>b</mi></mfrac></math></span>"
            "<span class='katex-html'>DUPLICATE_FORMULA</span></span> in the paper.</p>"
            + "<p>Selectable original paragraph for offline reading and persistent highlighting.</p>" * 12
            + f"<figure><img src='{image}' alt='Source figure'></figure>"
            "<svg viewBox='0 0 100 50' width='100' height='50'><rect x='1' y='1' width='98' height='48' fill='blue'/></svg>"
            "<canvas width='100' height='50'></canvas><script>window.PAPER_SCRIPT_EXECUTED=true</script>"
            "<p onclick='alert(1)'>Safe text</p></article></body></html>")


def png(color="red"):
    output = io.BytesIO()
    saved.Image.new("RGB", (80, 40), color).save(output, format="PNG")
    return output.getvalue()


def mhtml():
    message = EmailMessage(policy=policy.SMTP)
    message.set_type("multipart/related")
    html = EmailMessage()
    html.set_content(fixture("https://papers.example/figure.png"), subtype="html", charset="utf-8")
    html["Content-Location"] = "https://papers.example/article"
    message.attach(html)
    image = EmailMessage()
    image.set_content(png(), maintype="image", subtype="png")
    image["Content-Location"] = "https://papers.example/figure.png"
    message.attach(image)
    return message.as_bytes()


class SavedHtmlTests(unittest.TestCase):
    def setUp(self):
        self.vault = backend.new_vault(self._testMethodName)
        self.source = self.vault / "saved.html"
        self.source.write_text(fixture(), encoding="utf-8")
        (self.vault / "figure.png").write_bytes(png())
        self.storage = self.vault / "html_reader"

    def test_local_content_math_images_and_notices(self):
        before = self.source.read_bytes()
        with patch.object(reader, "_download", side_effect=AssertionError("network used")):
            document = saved.import_file(self.source, self.storage)
            root = self.storage / document["id"]
            page = reader.get_page(root, document["id"], 1, refresh=True)
            self.assertEqual("saved-html", page["source_kind"])
            self.assertEqual(1, document["math_count"])
            self.assertEqual(2, len(document["images"]))
            for key in document["images"]:
                payload, mime = reader.get_image(root, document["id"], key)
                self.assertTrue(payload)
                self.assertIn(mime, ("image/png", "image/svg+xml"))
        text = "".join(chunk["html"] for chunk in document["chunks"])
        for unsafe in ("<script", "onclick=", "<canvas", "<svg", "DUPLICATE_FORMULA", "saved-image/"):
            self.assertNotIn(unsafe, text)
        self.assertIn("<mfrac", text)
        self.assertIn("Canvas", " ".join(document["warnings"]))
        self.assertEqual(before, self.source.read_bytes())
        self.assertTrue(reader.LOCAL_ID_RE.fullmatch(document["id"]))

    def test_citation_shadow_popover_is_not_repeated_in_body(self):
        citation = ("<d-cite><template shadowmode='open'><d-hover-box>HIDDEN_BIBLIOGRAPHY</d-hover-box>"
                    "<span class='citation-number'>[7]</span></template></d-cite>")
        self.source.write_text(fixture().replace("</article>", citation + "</article>"), encoding="utf-8")
        document = saved.import_file(self.source, self.storage)
        html = "".join(chunk["html"] for chunk in document["chunks"])
        self.assertIn("[7]", html)
        self.assertNotIn("HIDDEN_BIBLIOGRAPHY", html)

    def test_archive_origin_is_part_of_document_identity(self):
        first = self.vault / "first.mhtml"
        second = self.vault / "second.mhtml"
        first.write_bytes(mhtml())
        second.write_bytes(mhtml().replace(b"https://papers.example/article", b"https://other.example/article"))
        one = saved.import_file(first, self.storage)
        two = saved.import_file(second, self.storage)
        self.assertNotEqual(one["id"], two["id"])
        self.assertTrue((self.storage / one["id"] / "article.json").is_file())

    def test_mhtml_relative_locations_resolve_after_root_html_is_selected(self):
        archive = BytesParser(policy=policy.default).parsebytes(mhtml())
        html, image = list(archive.iter_parts())
        html.set_content(fixture("figure.png"), subtype="html", charset="utf-8")
        html["Content-Location"] = "https://papers.example/paper/"
        image.replace_header("Content-Location", "figure.png")
        archive.set_payload([image, html])  # The image can precede the root HTML part.
        source = self.vault / "relative.mhtml"
        source.write_bytes(archive.as_bytes())
        with patch.object(reader, "_download", side_effect=AssertionError("network used")):
            document = saved.import_file(source, self.storage)
        self.assertEqual(2, len(document["images"]))
        self.assertFalse(any("缺失或未通过" in warning for warning in document["warnings"]))

    def test_mhtml_cid_images_still_resolve_without_content_location(self):
        archive = BytesParser(policy=policy.default).parsebytes(mhtml())
        html, image = list(archive.iter_parts())
        html.set_content(fixture("cid:figure@sample"), subtype="html", charset="utf-8")
        html["Content-Location"] = "https://papers.example/paper/"
        del image["Content-Location"]
        image["Content-ID"] = "<figure@sample>"
        source = self.vault / "cid.mhtml"
        source.write_bytes(archive.as_bytes())
        document = saved.import_file(source, self.storage)
        self.assertEqual(2, len(document["images"]))
        self.assertFalse(any("缺失或未通过" in warning for warning in document["warnings"]))

    def test_mhtml_uses_embedded_resources_only(self):
        source = self.vault / "saved.mhtml"
        source.write_bytes(mhtml())
        with patch.object(reader, "_download", side_effect=AssertionError("network used")):
            document = saved.import_file(source, self.storage)
        self.assertEqual("https://papers.example/article", document["source_url"])
        self.assertEqual(2, len(document["images"]))
        self.assertFalse(any("缺失或未通过" in warning for warning in document["warnings"]))

    def test_missing_external_and_escaping_resources_are_explicit(self):
        for value in ("https://evil.example/tracker.png", "../private.png", "%2e%2e/private.png", "file:///private.png"):
            self.source.write_text(fixture(value), encoding="utf-8")
            with patch.object(reader, "_download", side_effect=AssertionError("network used")):
                document = saved.import_file(self.source, self.storage)
            self.assertEqual(1, len(document["images"]))  # Only the inline SVG.
            self.assertTrue(any("缺失或未通过" in warning for warning in document["warnings"]))

    def test_unicode_relative_resource_folder(self):
        folder = self.vault / "网页_files"
        folder.mkdir()
        (folder / "图1.png").write_bytes(png())
        self.source.write_text(fixture("%E7%BD%91%E9%A1%B5_files/%E5%9B%BE1.png"), encoding="utf-8")
        document = saved.import_file(self.source, self.storage)
        self.assertEqual(2, len(document["images"]))

    def test_stable_identity_and_existing_annotations(self):
        document = saved.import_file(self.source, self.storage)
        root = self.storage / document["id"]
        run = next(r for r in document["runs"] if len(r["text"]) > 30)
        quote = run["text"][:20]
        mark = {"block": run["id"], "start": 0, "end": 20, "quote": quote}
        reader.save_highlights(root, document, {"source": document["source"], "highlights": [mark]})
        again = saved.import_file(self.source, self.storage)
        self.assertEqual(document["id"], again["id"])
        self.assertEqual([mark], reader.load_highlights(root, again)["highlights"])
        (self.vault / "figure.png").write_bytes(png("green"))
        changed = saved.import_file(self.source, self.storage)
        self.assertNotEqual(document["id"], changed["id"])
        self.assertEqual([mark], reader.load_highlights(root, document)["highlights"])

    def test_cache_only_ids_never_download_missing_content(self):
        document = saved.import_file(self.source, self.storage)
        root = self.storage / document["id"]
        key = next(iter(document["images"]))
        asset = next((root / "images").glob(key + ".*"))
        asset.rename(asset.with_suffix(".missing"))
        with patch.object(reader, "_download", side_effect=AssertionError("network used")):
            with self.assertRaises(ValueError):
                reader.get_image(root, document["id"], key)
            with self.assertRaises(reader.HtmlUnavailable):
                reader.get_document(root / "absent", document["id"], refresh=True)

    def test_mathjax_keeps_only_native_mathml(self):
        duplicate = ("<p><span class='MathJax'><nobr>MATHJAX_DUPLICATE</nobr>"
                     "<span class='MJX_Assistive_MathML'><math><mi>x</mi></math></span></span></p>")
        self.source.write_text(fixture().replace("</article>", duplicate + "</article>"), encoding="utf-8")
        document = saved.import_file(self.source, self.storage)
        html = "".join(chunk["html"] for chunk in document["chunks"])
        self.assertEqual(2, document["math_count"])
        self.assertNotIn("MATHJAX_DUPLICATE", html)
        self.assertNotIn("DUPLICATE_FORMULA", html)

    def test_svg_whitelist_and_canonical_case(self):
        data = b'''<svg xmlns:xlink="http://www.w3.org/1999/xlink" viewbox="0 0 20 20" onload="alert(1)">
<script>alert(1)</script><foreignObject><p>unsafe</p></foreignObject><image href="https://evil.example/a"/>
<defs><clippath id="clip"><rect width="10" height="10"/></clippath></defs>
<rect id="r" width="20" height="20" fill="url(https://evil.example/a)" style="stroke:red;background:url(https://evil.example/a)"/>
<use xlink:href="#r"/><use href="file:///private"/></svg>'''
        payload = saved.safe_svg(data).decode()
        for unsafe in ("<script", "foreignObject", "onload", "https://", "file://", "background"):
            self.assertNotIn(unsafe, payload)
        self.assertIn('viewBox="0 0 20 20"', payload)
        self.assertIn("clipPath", payload)
        self.assertIn('href="#r"', payload)
        self.assertIn("http://www.w3.org/2000/svg", payload)
        for xml in (b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///private">]><svg>&x;</svg>',
                    '<!DOCTYPE svg><svg/>'.encode("utf-16")):
            with self.assertRaises(ValueError):
                saved.safe_svg(xml)

    def test_reject_non_articles_and_arxiv_downloader_stays_restricted(self):
        self.source.write_text("<html><body>Error page</body></html>", encoding="utf-8")
        with self.assertRaises(ValueError):
            saved.import_file(self.source, self.storage)
        for url in ("https://papers.example/article", "http://arxiv.org/html/2510.16720"):
            with self.assertRaises(ValueError):
                reader._origin(url)

    def test_queue_reading_is_independent_and_duplicate_safe(self):
        item = app.add_queue_item(self.vault, "html", str(self.source))
        self.assertEqual("readable", item["status"])
        info = app.queue_reading_info(self.vault, item["id"])
        self.assertEqual(item["paper_id"], info["paper_id"])
        self.assertIsNone(info["arxiv"])
        with self.assertRaisesRegex(ValueError, "已在队列"):
            app.add_queue_item(self.vault, "html", str(self.source))
        with patch.object(app, "llama_health", side_effect=AssertionError("model used")), \
                patch.object(app, "llm_chat", side_effect=AssertionError("generation started")):
            self.assertEqual(item["paper_id"], app.queue_reading_info(self.vault, item["id"])["paper_id"])
        self.assertEqual("readable", app.load_queue(self.vault)[0]["status"])
        self.assertIn("reader=local-", app.reader_window_url({"reader": item["paper_id"], "view": "paper"}))


class SavedHtmlApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        backend.ApiTests.setUpClass()
        cls.api = backend.ApiTests("test_notes_and_html_contract")

    @classmethod
    def tearDownClass(cls):
        backend.ApiTests.tearDownClass()

    def test_import_read_images_and_annotations(self):
        vault = backend.ApiTests.vault
        source = vault / "local-api.html"
        source.write_text(fixture(), encoding="utf-8")
        (vault / "figure.png").write_bytes(png())
        status, result = self.api.request("/api/queue", {"kind": "html", "value": str(source)})
        self.assertEqual(200, status)
        item = result["item"]
        paper_id = item["paper_id"]
        status, info = self.api.request("/api/queue/reading?id=" + item["id"])
        self.assertEqual(200, status)
        self.assertEqual(paper_id, info["paper_id"])
        status, page = self.api.request("/api/paper?id=" + paper_id + "&page=1")
        self.assertEqual(200, status)
        self.assertEqual("saved-html", page["source_kind"])
        self.assertTrue(page["warnings"])
        _root, document, _legacy = app.html_reader_state(vault, paper_id)
        for key in document["images"]:
            self.assertEqual(200, self.api.request("/api/paper/html-image?id=" + paper_id + "&name=" + key)[0])
        run = next(r for r in document["runs"] if len(r["text"]) > 30)
        mark = {"block": run["id"], "start": 0, "end": 20, "quote": run["text"][:20]}
        body = {"source": document["source"], "highlights": [mark]}
        self.assertEqual(200, self.api.request("/api/reading-highlights?id=" + paper_id, body)[0])
        self.assertEqual([mark], self.api.request("/api/reading-highlights?id=" + paper_id)[1]["highlights"])
        self.assertEqual(400, self.api.request("/api/pdf?id=" + paper_id)[0])
        self.assertEqual(400, self.api.request("/api/paper?id=../../private")[0])


if __name__ == "__main__":
    print("Saved HTML test artifacts:", backend.RUN)
    unittest.main(verbosity=2)
