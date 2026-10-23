"""Offline whole-text coverage, evidence, hierarchy, source scope and failure safety."""
from __future__ import annotations

import json
import re
import unittest
from unittest.mock import MagicMock, patch

import test_app_backend as backend
from pdf_fixture import paper_fixture
from test_saved_html import fixture, png

app = backend.app
engine = app.fulltext


class FakeChat:
    def __init__(self):
        self.source_inputs = []
        self.merges = 0
        self.finals = 0

    def __call__(self, system, user, max_tokens):
        if "<source>" in user:
            text = user.split("<source>\n", 1)[1].split("\n</source>", 1)[0]
            self.source_inputs.append(re.sub(r"^\[S\d{3}\] ", "", text, flags=re.MULTILINE))
            return json.dumps({"summary": "本段内容摘要", "evidence": [{"point": "本段明确写出的内容", "passage": "S001"}]}, ensure_ascii=False)
        refs = sorted(set(engine.CITATION.findall(user)))
        if "全部 " in user:
            self.finals += 1
            return json.dumps({"sections": [{"heading": heading, "text": "全文文字导读，图表未作视觉分析。",
                                              "sources": [refs[0][1:-1]]} for heading in engine.SECTIONS]}, ensure_ascii=False)
        self.merges += 1
        return "合并段落证据。 " + " ".join(refs)


def source(text):
    return {"title": "Whole-text fixture", "kind": "saved-html", "source": "fixture-source",
            "parts": [{"label": "HTML part 1", "text": text}], "warnings": ["Figures are not analyzed"]}


class FullTextTests(unittest.TestCase):
    def setUp(self):
        self.vault = backend.new_vault(self._testMethodName)

    def test_all_text_including_tail_is_processed(self):
        original = ("Original paragraph about the method and its reported limitations. " * 200
                    + "UNIQUE_LAST_SECTION_EVIDENCE_MARKER states the final limitation.")
        chat = FakeChat()
        messages = []
        guide, ledger = engine.summarize(source(original), chat, 16384, messages.append)
        self.assertGreater(len(chat.source_inputs), 1)
        self.assertIn("UNIQUE_LAST_SECTION_EVIDENCE_MARKER", chat.source_inputs[-1])
        self.assertEqual("".join(original.split()), "".join("".join(chat.source_inputs).split()))
        self.assertEqual(len(chat.source_inputs), len(ledger["chunks"]))
        self.assertEqual(1, chat.finals)
        self.assertIn("[C0001]", guide)
        self.assertTrue(all(len(text) <= ledger["chunk_char_budget"] for text in chat.source_inputs))
        self.assertIn("生成全文文字导读", messages)

    def test_tiny_tail_is_not_lost_or_emitted_as_one_character(self):
        text = "a" * 6001
        chunks = engine.split_parts([{"label": "part", "text": text}], 6000)
        self.assertEqual(text, "".join(chunk["text"] for chunk in chunks))
        self.assertGreaterEqual(len(chunks[-1]["text"]), 100)
        with self.assertRaises(ValueError):
            engine.split_parts([{"label": "part", "text": text}], 0)

    def test_hierarchy_uses_bounded_groups_without_skipping_source(self):
        text = "A finding with original evidence and a clear limitation. " * 3000
        chat = FakeChat()
        _guide, ledger = engine.summarize(source(text), chat, 8192)
        self.assertGreater(chat.merges, 0)
        self.assertEqual("".join(text.split()), "".join("".join(chat.source_inputs).split()))
        self.assertEqual(len(chat.source_inputs), len(ledger["chunks"]))

    def test_unknown_passage_and_bad_json_fail_with_bounded_retries(self):
        for output in ("not JSON", json.dumps({"summary": "Claim", "evidence": [{"point": "Claim", "passage": "S999"}]})):
            calls = []
            def invalid_chat(system, user, tokens, calls=calls, output=output):
                calls.append(user)
                return output
            with self.assertRaisesRegex(ValueError, "未通过校验"):
                engine.summarize(source("Original source text only. " * 20), invalid_chat, 16384)
            self.assertEqual(2, len(calls))

    def test_multiple_passage_selection_uses_source_not_model_quotes(self):
        passages = {f"S{i:03d}": f"Original source passage number {i} with distinct evidence." for i in range(1, 17)}
        chunk = {"text": " ".join(passages.values())}
        raw = json.dumps({"summary": "A summary", "evidence": [
            {"point": "A point supported by several source passages", "passages": list(passages),
             "quote": "FABRICATED MODEL QUOTE"}]})
        checked = engine._checked_chunk(raw, chunk, passages, 2500)
        self.assertEqual(list(passages.values()), checked["evidence"][0]["quotes"])
        self.assertNotIn("FABRICATED MODEL QUOTE", json.dumps(checked))
        raw = json.dumps({"summary": "A summary", "evidence": [{"point": "Claim", "passages": ["S999"]}]})
        with self.assertRaises(ValueError):
            engine._checked_chunk(raw, chunk, passages, 2500)

    def test_structured_final_can_select_multiple_known_sources(self):
        allowed = {f"[C{i:04d}]" for i in range(1, 17)}
        raw = json.dumps({"sections": [{"heading": heading, "text": "Supported text",
                                      "sources": sorted(allowed)} for heading in engine.SECTIONS]})
        note = engine._checked_final(raw, allowed)
        self.assertIn("[C0016]", note)
        self.assertEqual(5, note.count("## "))

    def test_corrupt_pdf_queue_path_cannot_move_an_external_file(self):
        source_path = self.vault / "2510.00010.pdf"
        source_path.write_bytes(paper_fixture())
        item = app.add_queue_item(self.vault, "pdf", str(source_path))
        with patch.object(app, "fetch_arxiv", side_effect=AssertionError("abstract API used")), \
                patch.object(app.urllib.request, "urlopen", side_effect=AssertionError("network used")):
            _paper_id, extracted = app.prepare_fulltext_source(self.vault, item)
        self.assertEqual("pdf-text", extracted["kind"])
        item["stored"] = "../../2510.00010.pdf"
        with self.assertRaisesRegex(ValueError, "原文件未移动"):
            app.prepare_fulltext_source(self.vault, item)
        self.assertTrue(source_path.is_file())

    def test_unknown_final_citations_and_empty_output_fail(self):
        bad_source = json.dumps({"sections": [{"heading": heading, "text": "Unsupported",
                                              "sources": ["C9999"]} for heading in engine.SECTIONS]})
        for final in ("", "Unsupported result [C9999]", "A note without evidence IDs", bad_source):
            good = FakeChat()
            def chat(system, user, tokens, good=good, final=final):
                return good(system, user, tokens) if "<source>" in user else final
            with self.assertRaises(ValueError):
                engine.summarize(source("Original source text only. " * 20), chat, 16384)

    def test_small_context_and_overlong_sources_are_not_truncated(self):
        chat = FakeChat()
        with self.assertRaisesRegex(ValueError, "至少 8192"):
            engine.summarize(source("A full paper. " * 20), chat, 4096)
        self.assertEqual([], chat.source_inputs)
        with self.assertRaisesRegex(ValueError, "128"):
            engine.split_parts([{"label": "part", "text": "x" * (257 * 128)}], 256)

    def test_arxiv_html_uses_all_cached_text_not_abstract_api(self):
        from html_fixture import seed_html

        paper_id = "2510.00011"
        document = seed_html(self.vault, paper_id)
        item = app.add_queue_item(self.vault, "link", paper_id)
        with patch.object(app, "fetch_arxiv", side_effect=AssertionError("abstract API used")), \
                patch.object(app.paper_html, "_download", side_effect=AssertionError("network used")):
            identifier, extracted = app.prepare_fulltext_source(self.vault, item)
        self.assertEqual(paper_id, identifier)
        self.assertEqual("arxiv-html", extracted["kind"])
        self.assertEqual(len(document["chunks"]), len(extracted["parts"]))
        self.assertIn("End of the structured article", extracted["parts"][-1]["text"])

    def test_arxiv_html_failure_falls_back_to_pdf_text_explicitly(self):
        paper_id = "2510.00012"
        cache = self.vault / "papers_pdf"
        cache.mkdir()
        (cache / (paper_id + ".pdf")).write_bytes(paper_fixture())
        item = app.add_queue_item(self.vault, "link", paper_id)
        with patch.object(app.paper_html, "get_document", side_effect=app.paper_html.HtmlUnavailable("No HTML")), \
                patch.object(app.urllib.request, "urlopen", side_effect=AssertionError("network used")):
            _identifier, extracted = app.prepare_fulltext_source(self.vault, item)
        self.assertEqual("pdf-text", extracted["kind"])
        self.assertIn("不是摘要", extracted["warnings"][0])

    def test_missing_full_text_does_not_silently_generate_from_abstract(self):
        app.add_queue_item(self.vault, "link", "2510.00013")
        with patch.object(app, "llama_health", return_value=True), \
                patch.object(app.paper_html, "get_document", side_effect=app.paper_html.HtmlUnavailable("No HTML")), \
                patch.object(app, "get_pdf_path", side_effect=ValueError("No PDF")), \
                patch.object(app, "write_paper_note", side_effect=AssertionError("abstract fallback used")):
            result = app.process_queue(self.vault)
        self.assertEqual(1, result["failed"])
        self.assertFalse(list((self.vault / "papers").glob("*.md")))

    def test_html_source_keeps_tail_and_labels_visual_limits(self):
        document = {"title": "HTML fixture", "source": "html-v1-fixture", "warnings": ["Canvas missing"],
                    "chunks": [{"html": "<p>First part <math alttext='x^2'><msup><mi>x</mi><mn>2</mn></msup></math><img alt='Chart'></p>"},
                               {"html": "<p>UNIQUE_HTML_LAST_PART</p>"}]}
        result = engine.html_source(document, "saved-html")
        self.assertEqual(2, len(result["parts"]))
        self.assertIn("x^2", result["parts"][0]["text"])
        self.assertIn("图片未供模型读取", result["parts"][0]["text"])
        self.assertIn("UNIQUE_HTML_LAST_PART", result["parts"][-1]["text"])
        self.assertIn("Canvas missing", result["warnings"])

    def test_pdf_all_pages_and_image_only_page_is_explicit_failure(self):
        path = self.vault / "fixture.pdf"
        path.write_bytes(paper_fixture())
        result = engine.pdf_source(path, "Local fixture")
        self.assertTrue(result["parts"])
        self.assertEqual("pdf-text", result["kind"])
        self.assertIn("PDF page 1", result["parts"][0]["label"])
        with patch.object(engine.pdfplumber, "open") as opening:
            pdf = opening.return_value.__enter__.return_value
            pdf.metadata = {}
            blank = MagicMock()
            blank.extract_text.return_value = ""
            pdf.pages = [blank]
            with self.assertRaisesRegex(ValueError, "没有可提取文字"):
                engine.pdf_source(path, "Local fixture")

    def test_saved_queue_generates_full_text_note_and_local_reader_identity(self):
        path = self.vault / "paper.html"
        path.write_text(fixture(), encoding="utf-8")
        (self.vault / "figure.png").write_bytes(png())
        item = app.add_queue_item(self.vault, "html", str(path))
        chat = FakeChat()
        with patch.object(app, "llama_health", return_value=True), patch.object(app, "llm_chat", side_effect=chat), \
                patch.object(app, "fetch_arxiv", side_effect=AssertionError("abstract API used")):
            result = app.process_queue(self.vault)
        self.assertEqual(1, result["processed"])
        done = app.load_queue(self.vault)[0]
        self.assertEqual("done", done["status"])
        note = (self.vault / "papers" / done["note"]).read_text(encoding="utf-8")
        self.assertIn("- Paper ID: " + item["paper_id"], note)
        self.assertIn("saved-html; all extracted text chunks processed", note)
        self.assertIn("## 原文引用依据", note)
        self.assertNotIn("abstract only", note)
        self.assertIsNone(done["progress"])
        self.assertTrue(list((self.vault / "fulltext").glob("*.json")))

    def test_failed_fulltext_generation_preserves_old_note_and_reading(self):
        path = self.vault / "paper.html"
        path.write_text(fixture(), encoding="utf-8")
        item = app.add_queue_item(self.vault, "html", str(path))
        old = self.vault / "papers" / f"{item['paper_id']}-Saved-paper-fixture.md"
        old.write_text("Existing good note", encoding="utf-8")
        with patch.object(app, "llama_health", return_value=True), patch.object(app, "llm_chat", return_value="Invalid JSON"):
            result = app.process_queue(self.vault)
        self.assertEqual(1, result["failed"])
        self.assertEqual("Existing good note", old.read_text(encoding="utf-8"))
        info = app.queue_reading_info(self.vault, item["id"])
        self.assertEqual(item["paper_id"], info["paper_id"])
        _root, document, _legacy = app.html_reader_state(self.vault, info["paper_id"])
        self.assertTrue(document["chunks"])


class FullTextApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        backend.ApiTests.setUpClass()
        cls.api = backend.ApiTests("test_notes_and_html_contract")

    @classmethod
    def tearDownClass(cls):
        backend.ApiTests.tearDownClass()

    def test_local_note_identifier_is_not_guessed_from_cited_arxiv_numbers(self):
        paper_id = "local-" + "a" * 24
        path = backend.ApiTests.vault / "papers" / "full-local.md"
        path.write_text("# Imported paper\n\n- Paper ID: " + paper_id + "\n\nCites arXiv 2510.16720.", encoding="utf-8")
        status, note = self.api.request("/api/note?kind=papers&name=full-local.md")
        self.assertEqual(200, status)
        self.assertEqual(paper_id, note["paper_id"])
        self.assertIsNone(note["arxiv"])


if __name__ == "__main__":
    print("Full-text test artifacts:", backend.RUN)
    unittest.main(verbosity=2)
