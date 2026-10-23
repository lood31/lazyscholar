"""Reversible whole-paper deletion, path boundaries and interrupted-operation recovery."""
from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import patch

import test_app_backend as backend

app = backend.app
store = app.paper_trash
PAPER = "2510.54321"
OTHER = "2510.54322"


class PaperTrashTests(unittest.TestCase):
    def setUp(self):
        self.vault = backend.new_vault(self._testMethodName)
        self.name = PAPER + "-fixture.md"
        self.row = {"id": "a1b2c3d4e5", "kind": "pdf", "label": PAPER + ".pdf", "value": "external-original.pdf",
                    "stored": "a1b2c3d4e5.pdf", "status": "done", "note": self.name}
        self.items = [self.row, {"id": "b1b2c3d4e5", "kind": "link", "label": OTHER, "value": OTHER, "status": "pending"}]
        app.save_queue(self.vault, self.items)
        self.expected = {
            "papers/" + self.name: ("# Fixture\n\n- Paper ID: " + PAPER + "\n\nGuide.").encode(),
            "papers_pdf/" + PAPER + ".pdf": b"original cached PDF bytes",
            "reader/" + PAPER + "/doc.json": b"legacy text cache",
            "html_reader/" + PAPER + "/article.json": b"HTML text cache",
            "html_reader/" + PAPER + "/images/figure.png": b"image bytes",
            "highlights/" + PAPER + ".json": b"original annotation bytes",
            "fulltext/" + PAPER + "-0123456789abcdef.json": b"fulltext evidence bytes",
            "queue_files/a1b2c3d4e5.pdf": b"owned upload copy",
            "processed/a1b2c3d4e5-" + PAPER + ".pdf": b"owned processed copy",
        }
        for relative, content in self.expected.items():
            path = self.vault / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.unrelated = self.vault / "papers" / "unrelated.md"
        self.unrelated.write_text("# Personal note\n\nA citation: https://arxiv.org/abs/" + PAPER, encoding="utf-8")
        self.external = backend.new_vault("external-original") / "original.pdf"
        self.external.write_bytes(b"external original must remain untouched")
        self.row["value"] = str(self.external)
        app.save_queue(self.vault, self.items)

    def save(self, values):
        app.save_queue(self.vault, values)

    def trash(self, **kwargs):
        with store.changing(self.vault):
            return store.trash(self.vault, app.load_queue(self.vault), self.save, **(kwargs or {"name": self.name}))

    def restore(self, entry):
        with store.changing(self.vault):
            return store.restore(self.vault, entry, app.load_queue(self.vault), self.save)

    def assert_originals(self):
        for relative, content in self.expected.items():
            self.assertEqual(content, (self.vault / relative).read_bytes(), relative)
        self.assertEqual(b"external original must remain untouched", self.external.read_bytes())
        self.assertTrue(self.unrelated.is_file())

    def test_whole_paper_and_queue_restore_byte_for_byte_without_touching_sources(self):
        result = self.trash()
        self.assertEqual(PAPER, result["paper_id"])
        for relative in self.expected:
            self.assertFalse((self.vault / relative).exists(), relative)
        self.assertEqual([self.items[1]], app.load_queue(self.vault))
        self.assertTrue(self.external.is_file())
        self.assertTrue(self.unrelated.is_file())
        self.assertEqual(1, len(store.list_entries(self.vault)))
        with self.assertRaises(store.Gone):
            store.ensure_active(self.vault, PAPER)
        self.restore(result["id"])
        self.assert_originals()
        self.assertEqual(self.items, sorted(app.load_queue(self.vault), key=lambda row: row["id"]))
        self.assertEqual([], store.list_entries(self.vault))
        store.ensure_active(self.vault, PAPER)

    def test_versioned_pdf_queue_uses_the_existing_base_identity(self):
        self.items[0]["label"] = PAPER + "v2.pdf"
        old = "processed/a1b2c3d4e5-" + PAPER + ".pdf"
        versioned = "processed/a1b2c3d4e5-" + PAPER + "v2.pdf"
        (self.vault / old).rename(self.vault / versioned)
        self.expected[versioned] = self.expected.pop(old)
        app.save_queue(self.vault, self.items)
        result = self.trash(queue_item=self.row["id"])
        self.assertEqual(PAPER, result["paper_id"])
        for relative in self.expected:
            self.assertFalse((self.vault / relative).exists(), relative)
        with self.assertRaises(store.Gone):
            app.add_queue_item(self.vault, "link", PAPER + "v3")
        self.restore(result["id"])
        self.assert_originals()

    def test_note_trash_collects_versioned_links_and_metadata(self):
        self.items[0] = {"id": "a1b2c3d4e5", "kind": "link", "value": "https://arxiv.org/abs/" + PAPER + "v2",
                         "label": PAPER + "v2", "status": "pending"}
        app.save_queue(self.vault, self.items)
        versioned = self.vault / "papers" / (PAPER + "v2-legacy.md")
        versioned.write_text("# Versioned legacy note\n\n- arXiv: https://arxiv.org/abs/" + PAPER + "v2", encoding="utf-8")
        result = self.trash()
        self.assertEqual([self.items[1]], app.load_queue(self.vault))
        self.assertFalse(versioned.exists())
        with self.assertRaises(store.Gone):
            store.ensure_active(self.vault, PAPER + "v2")
        self.restore(result["id"])
        self.assertTrue(versioned.exists())

    def test_inbox_generation_holds_lifecycle_gate_until_note_is_saved(self):
        entered, release = threading.Event(), threading.Event()
        src = self.vault / "inbox" / "paper.txt"
        src.write_text("https://arxiv.org/abs/" + PAPER + "v2", encoding="utf-8")
        meta = {"title": "fixture", "authors": ["Fixture Author"], "summary": "An abstract.", "url": "https://arxiv.org/abs/" + PAPER}
        errors = []
        def slow_chat(*args, **kwargs):
            entered.set()
            release.wait(timeout=10)
            return "A generated guide."
        def generate():
            try:
                app.process_file(self.vault, self.vault / "inbox", self.vault / "processed", src)
            except Exception as exc:
                errors.append(str(exc))
                raise
        with patch.object(app, "fetch_arxiv", return_value=meta), patch.object(app, "llm_chat", side_effect=slow_chat):
            worker = threading.Thread(target=generate)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=5))
                with self.assertRaises(store.Busy):
                    self.trash()
            finally:
                release.set()
                worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        self.assertFalse(src.exists())
        result = self.trash()
        self.assertFalse((self.vault / "papers" / self.name).exists())
        self.restore(result["id"])

    def test_trashed_inbox_paper_is_rejected_before_fetch_or_model_call(self):
        self.trash()
        src = self.vault / "inbox" / "paper.txt"
        src.write_text("https://arxiv.org/abs/" + PAPER + "v2", encoding="utf-8")
        with patch.object(app, "fetch_arxiv") as fetch, patch.object(app, "llm_chat") as chat:
            with self.assertRaises(store.Gone):
                app.process_file(self.vault, self.vault / "inbox", self.vault / "processed", src)
            fetch.assert_not_called()
            chat.assert_not_called()
        self.assertTrue(src.is_file())
        self.assertFalse((self.vault / "papers" / self.name).exists())

    def test_pending_link_can_be_trashed_even_without_cached_files(self):
        result = self.trash(queue_item=self.items[1]["id"])
        self.assertEqual(OTHER, result["paper_id"])
        self.assertEqual([], result["paths"])
        self.restore(result["id"])
        self.assertEqual(2, len(app.load_queue(self.vault)))

    def test_reference_in_an_unrelated_note_is_not_paper_identity(self):
        with self.assertRaises(ValueError):
            self.trash(name=self.unrelated.name)
        self.assert_originals()
        self.assertEqual([], store.list_entries(self.vault))

    def test_restore_conflict_never_overwrites_current_files(self):
        result = self.trash()
        target = self.vault / "papers" / self.name
        target.write_bytes(b"new unrelated contents at the destination")
        with self.assertRaises(store.Busy):
            self.restore(result["id"])
        self.assertEqual(b"new unrelated contents at the destination", target.read_bytes())
        self.assertFalse((self.vault / "papers_pdf" / (PAPER + ".pdf")).exists())
        self.assertEqual(1, len(store.list_entries(self.vault)))

    def test_second_move_failure_rolls_back_files_queue_and_tombstone(self):
        original_move = store._move
        calls = 0
        def fail_second(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated permission failure")
            original_move(source, destination)
        with patch.object(store, "_move", side_effect=fail_second), self.assertRaises(OSError):
            self.trash()
        self.assert_originals()
        self.assertEqual(self.items, app.load_queue(self.vault))
        self.assertEqual([], store.list_entries(self.vault))

    def test_interrupted_move_can_be_recovered_from_its_durable_manifest(self):
        original_move = store._move
        def crash(source, destination):
            original_move(source, destination)
            raise KeyboardInterrupt("simulate abrupt process stop after one rename")
        with patch.object(store, "_move", side_effect=crash), self.assertRaises(KeyboardInterrupt):
            self.trash()
        entry = store.list_entries(self.vault)[0]
        self.assertEqual("moving", entry["state"])
        self.restore(entry["id"])
        self.assert_originals()
        self.assertEqual(self.items, app.load_queue(self.vault))

    def test_manifest_cannot_restore_paths_outside_paper_data(self):
        result = self.trash()
        manifest = self.vault / store.ROOT / result["id"] / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        for relative in ("../outside.md", "config.json", "papers/../config.json", "papers/C:secret.md"):
            data["paths"] = [relative]
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                self.restore(result["id"])
        self.assertFalse((self.vault / "papers" / self.name).exists())
        self.assertTrue(self.external.is_file())

    def test_corrupt_queue_source_path_is_rejected_before_any_move(self):
        self.items[0]["stored"] = "../external-original.pdf"
        app.save_queue(self.vault, self.items)
        with self.assertRaises(ValueError):
            self.trash()
        self.assert_originals()
        self.assertEqual([], store.list_entries(self.vault))

    def test_processing_paper_cannot_be_trashed(self):
        self.items[0]["status"] = "processing"
        app.save_queue(self.vault, self.items)
        with self.assertRaises(store.Busy):
            self.trash()
        self.assert_originals()

    def test_active_requests_block_changes_without_serializing_other_readers(self):
        entered, release = threading.Event(), threading.Event()
        def hold_reader():
            with store.reading(self.vault):
                entered.set()
                release.wait(timeout=10)
        worker = threading.Thread(target=hold_reader)
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=5))
            with store.reading(self.vault), self.assertRaises(store.Busy):
                self.trash()
        finally:
            release.set()
            worker.join(timeout=5)
        result = self.trash()
        self.restore(result["id"])
        self.assert_originals()

    def test_model_health_poll_does_not_hold_the_file_movement_gate(self):
        entered, release = threading.Event(), threading.Event()
        def slow_health(*args, **kwargs):
            entered.set()
            release.wait(timeout=10)
            raise OSError("simulated slow/offline model")
        with patch.object(app.urllib.request, "urlopen", side_effect=slow_health):
            worker = threading.Thread(target=lambda: app.vault_state(self.vault))
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=5))
                with store.changing(self.vault):
                    pass
            finally:
                release.set()
                worker.join(timeout=5)
        self.assert_originals()

    def test_interrupted_trash_is_hidden_and_not_processed_by_the_scheduler(self):
        pending = {**self.row, "status": "pending"}
        app.save_queue(self.vault, [pending])
        original_move = store._move
        def crash(source, destination):
            original_move(source, destination)
            raise KeyboardInterrupt("simulated process stop")
        with patch.object(store, "_move", side_effect=crash), self.assertRaises(KeyboardInterrupt):
            self.trash()
        self.assertEqual([], app.active_queue(self.vault))
        with self.assertRaises(store.Gone):
            app.queue_reading_info(self.vault, pending["id"])
        with patch.object(app, "_process_queue_item") as process, patch.object(app, "llama_health", return_value=True):
            result = app.process_queue(self.vault)
            process.assert_not_called()
        self.assertEqual({"ok": True, "processed": 0, "failed": 0}, result)
        self.assertEqual([pending], app.load_queue(self.vault))
        self.restore(store.list_entries(self.vault)[0]["id"])
        self.assert_originals()

    def test_symlink_in_cache_is_rejected_without_touching_its_target(self):
        link = self.vault / "html_reader" / PAPER / "images" / "external-link.png"
        try:
            link.symlink_to(self.external)
        except OSError:
            self.skipTest("Windows account cannot create symlinks")
        try:
            with self.assertRaises(ValueError):
                self.trash()
            self.assert_originals()
        finally:
            link.unlink()

    def test_html_reimport_checks_tombstone_before_writing_any_cache(self):
        source = self.vault / "saved.html"
        source.write_text("<title>Local fixture</title><article>" + "<p>Saved static body with useful context and a reported limitation.</p>" * 20
                          + "</article>", encoding="utf-8")
        row = app.add_queue_item(self.vault, "html", str(source))
        result = self.trash(queue_item=row["id"])
        self.assertFalse((self.vault / "html_reader" / row["paper_id"]).exists())
        with self.assertRaises(store.Gone):
            app.add_queue_item(self.vault, "html", str(source))
        self.assertFalse((self.vault / "html_reader" / row["paper_id"]).exists())
        self.restore(result["id"])
        self.assertTrue((self.vault / "html_reader" / row["paper_id"]).is_dir())
        self.assertTrue(source.is_file())


if __name__ == "__main__":
    unittest.main()
