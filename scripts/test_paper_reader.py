"""Deterministic reflow/annotation regressions; no model and no network."""
from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

from pdf_fixture import paper_fixture, pdf_bytes

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import paper_reader as reader  # noqa: E402

RUN = ROOT / "qa" / "reader-tests" / uuid.uuid4().hex[:10]


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.root = RUN / self._testMethodName
        self.root.mkdir(parents=True)
        self.pdf = self.root / "paper.pdf"
        self.pdf.write_bytes(paper_fixture())
        self.cache = self.root / "cache"

    def test_text_spaces_reading_order_and_native_regions(self):
        page = reader.extract_page(self.pdf, self.cache, 1)
        text = " ".join(b.get("text", "") for b in page["blocks"])
        self.assertIn("Left paragraph line 0 with readable spaces.", text)
        self.assertLess(text.index("Left paragraph line 9"), text.index("Right paragraph line 0"))
        kinds = {b["kind"] for b in page["blocks"]}
        self.assertTrue({"figure", "table", "equation"} <= kinds, kinds)
        self.assertEqual(1, page["pages"])
        for block in page["blocks"]:
            if "image" in block:
                path = reader.image_path(self.pdf, self.cache, block["image"])
                self.assertTrue(path.read_bytes().startswith(b"\x89PNG"))
        self.assertEqual(paper_fixture(), self.pdf.read_bytes())

    def test_cache_is_stable_and_invalidated_when_pdf_changes(self):
        first = reader.extract_page(self.pdf, self.cache, 1)
        path = reader.cache_for(self.pdf, self.cache) / "page-1.json"
        before = path.stat().st_mtime_ns
        self.assertEqual(first, reader.extract_page(self.pdf, self.cache, 1))
        self.assertEqual(before, path.stat().st_mtime_ns)
        self.pdf.write_bytes(pdf_bytes("BT /F1 12 Tf 40 700 Td (Changed paper with more words) Tj ET"))
        second = reader.extract_page(self.pdf, self.cache, 1)
        self.assertNotEqual(first["source"], second["source"])
        self.assertTrue(path.exists())  # Old annotation/cache generations are retained.

    def test_text_highlights_roundtrip_validation_and_clear(self):
        page = reader.extract_page(self.pdf, self.cache, 1)
        block = next(b for b in page["blocks"] if b.get("text", "").startswith("Left paragraph"))
        h = {"block": block["id"], "start": 0, "end": 4, "quote": "Left"}
        saved = reader.save_highlights(self.pdf, self.cache, {"source": page["source"], "highlights": [h]})
        self.assertEqual(saved, reader.load_highlights(self.pdf, self.cache))
        for rows in ([{**h, "quote": "Fake"}], [{**h, "start": -1}], [{**h, "block": "../../bad"}]):
            with self.assertRaises(ValueError):
                reader.save_highlights(self.pdf, self.cache, {"source": page["source"], "highlights": rows})
        with self.assertRaises(ValueError):
            reader.save_highlights(self.pdf, self.cache, {"source": "old-pdf", "highlights": []})
        self.assertEqual([h], reader.load_highlights(self.pdf, self.cache)["highlights"])
        reader.save_highlights(self.pdf, self.cache, {"source": page["source"], "highlights": []})
        self.assertEqual([], reader.load_highlights(self.pdf, self.cache)["highlights"])

    def test_utf16_offsets_match_webview_selections(self):
        page = reader.extract_page(self.pdf, self.cache, 1)
        block = next(b for b in page["blocks"] if "text" in b)
        block["text"] = "A😀BC"
        reader._write_json(reader.cache_for(self.pdf, self.cache) / "page-1.json", page)
        h = {"block": block["id"], "start": 1, "end": 3, "quote": "😀"}
        reader.save_highlights(self.pdf, self.cache, {"source": page["source"], "highlights": [h]})
        self.assertEqual([h], reader.load_highlights(self.pdf, self.cache)["highlights"])

    def test_scanned_pages_are_explicit_image_fallback_not_invented_text(self):
        self.pdf.write_bytes(pdf_bytes("q 300 0 0 300 100 200 cm /Im1 Do Q"))
        page = reader.extract_page(self.pdf, self.cache, 1)
        self.assertTrue(page["scanned"])
        self.assertTrue(any(b["kind"] == "scanned" for b in page["blocks"]))
        self.assertFalse(any(b.get("text") for b in page["blocks"]))

    def test_hard_hyphens_are_not_silently_removed(self):
        self.pdf.write_bytes(pdf_bytes("BT /F1 12 Tf 40 700 Td (A state-) Tj 0 -15 Td (of-the-art system.) Tj ET"))
        page = reader.extract_page(self.pdf, self.cache, 1)
        text = " ".join(b.get("text", "") for b in page["blocks"])
        self.assertIn("state-of-the-art", text)
        self.assertNotIn("stateof", text)

    def test_rotated_text_is_preserved_as_an_original_crop(self):
        self.pdf.write_bytes(pdf_bytes("BT /F1 12 Tf 40 700 Td (Normal readable prose.) Tj ET q 0 1 -1 0 100 200 cm BT /F1 12 Tf 0 0 Td (Rotated source note) Tj ET Q"))
        page = reader.extract_page(self.pdf, self.cache, 1)
        self.assertTrue(any(b.get("text") == "Normal readable prose." for b in page["blocks"]))
        self.assertTrue(any(b["kind"] == "rotated" and b.get("image") for b in page["blocks"]))

    def test_identical_pdf_reimport_keeps_highlights(self):
        page = reader.extract_page(self.pdf, self.cache, 1)
        block = next(b for b in page["blocks"] if b.get("text", "").startswith("Left paragraph"))
        h = {"block":block["id"], "start":0, "end":4, "quote":"Left"}
        reader.save_highlights(self.pdf,self.cache,{"source":page["source"],"highlights":[h]})
        content = self.pdf.read_bytes()
        self.pdf.write_bytes(content)
        self.assertEqual(page["source"], reader.extract_page(self.pdf,self.cache,1)["source"])
        self.assertEqual([h],reader.load_highlights(self.pdf,self.cache)["highlights"])

    def test_invalid_page_and_asset_paths_fail(self):
        for page in (0, 2):
            with self.assertRaises(ValueError):
                reader.extract_page(self.pdf, self.cache, page)
        with self.assertRaises(ValueError):
            reader.image_path(self.pdf, self.cache, "../../secret.png")


if __name__ == "__main__":
    print(f"Reader test artifacts: {RUN}", flush=True)
    unittest.main(verbosity=2)
