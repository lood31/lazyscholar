"""Native recycle lifecycle with an isolated fake OS provider; never touches the real bin."""
from __future__ import annotations

import json
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import test_app_backend as backend

import windows_recycle as native

app = backend.app
store = app.paper_trash
PAPER = "2510.54321"
OTHER = "2510.54322"


class FakeBin:
    def __init__(self, root):
        self.root = root
        self.moves = {}
        self.calls = 0
        self.fail_after: int | None = None

    def recycle(self, paths):
        self.calls += 1
        result = native.Result()
        for index, source in enumerate(paths):
            if self.fail_after is not None and index >= self.fail_after:
                result.error = "simulated native refusal"
                break
            destination = self.root / (uuid.uuid4().hex + "-" + source.name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)
            self.moves[str(source)] = str(destination)
            result.completed[str(source)] = str(destination)
        return result

    def restore(self, only=None):
        for original, recycled in self.moves.items():
            if only and original not in only:
                continue
            target, cached = Path(original), Path(recycled)
            if not cached.exists():
                continue
            if target.exists():
                raise FileExistsError("Test restore never overwrites")
            target.parent.mkdir(parents=True, exist_ok=True)
            cached.rename(target)


class PaperTrashTests(unittest.TestCase):
    def setUp(self):
        self.vault = backend.new_vault(self._testMethodName)
        self.name = PAPER + "-fixture.md"
        self.row = {"id": "a1b2c3d4e5", "kind": "pdf", "label": PAPER + ".pdf", "value": "external-original.pdf",
                    "stored": "a1b2c3d4e5.pdf", "status": "done", "note": self.name}
        self.items = [self.row, {"id": "b1b2c3d4e5", "kind": "link", "label": OTHER, "value": OTHER, "status": "pending"}]
        self.expected = {
            "papers/" + self.name: ("# Fixture\n\n- Paper ID: " + PAPER + "\n\nGuide.").encode(),
            "papers_pdf/" + PAPER + ".pdf": b"%PDF-1.4 original cached PDF bytes",
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
        self.bin = FakeBin(backend.new_vault(self._testMethodName + "-fake-system-bin"))
        self.patcher = patch.object(native, "recycle", side_effect=self.bin.recycle)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def save(self, values):
        app.save_queue(self.vault, values)

    def preview(self, **selection):
        with store.reading(self.vault):
            return store.preview(self.vault, app.load_queue(self.vault), **(selection or {"name": self.name}))

    def trash(self, **selection):
        selection = selection or {"name": self.name}
        preview = self.preview(**selection)
        with store.changing(self.vault):
            return store.trash(self.vault, app.load_queue(self.vault), self.save, token=preview["token"], **selection)

    def reconcile(self):
        return store.reconcile(self.vault, app.load_queue(self.vault), self.save)

    def assert_originals(self):
        for relative, content in self.expected.items():
            self.assertEqual(content, (self.vault / relative).read_bytes(), relative)
        self.assertEqual(b"external original must remain untouched", self.external.read_bytes())
        self.assertTrue(self.unrelated.is_file())

    def test_native_provider_moves_whole_paper_but_no_private_payload_is_created(self):
        result = self.trash()
        self.assertTrue(result["ok"])
        for relative in self.expected:
            self.assertFalse((self.vault / relative).exists(), relative)
        self.assertFalse((self.vault / store.ROOT).exists())
        self.assertTrue(self.external.is_file())
        self.assertTrue(self.unrelated.is_file())
        self.assertEqual([self.items[1]], app.load_queue(self.vault))
        with self.assertRaises(store.Gone):
            store.ensure_active(self.vault, PAPER)
        self.bin.restore()
        self.assertEqual([PAPER], self.reconcile())
        self.assert_originals()
        self.assertEqual(self.items, sorted(app.load_queue(self.vault), key=lambda row: row["id"]))

    def test_cancelled_or_failed_native_call_never_falls_back_to_deleting(self):
        self.bin.fail_after = 0
        result = self.trash()
        self.assertFalse(result["ok"])
        self.assertFalse(result["partial"])
        self.assert_originals()
        self.assertEqual(self.items, app.load_queue(self.vault))
        self.assertNotIn(PAPER, store.deleted_ids(self.vault))
        self.assertEqual("", store.lifecycle_epoch(self.vault, PAPER))

    def test_partial_success_retains_receipt_and_queue_then_retries_only_remaining_paths(self):
        self.bin.fail_after = 1
        first = self.trash()
        self.assertTrue(first["partial"])
        self.assertEqual(1, first["completed"])
        self.assertEqual(self.items, app.load_queue(self.vault))
        self.assertEqual([self.items[1]], app.active_queue(self.vault))
        self.assertNotIn(self.row, app.active_queue(self.vault))
        self.assertTrue(store.attention(self.vault)["pending"])
        self.bin.fail_after = None
        final = self.trash(receipt_id=first["id"])
        self.assertTrue(final["ok"])
        self.bin.restore()
        self.reconcile()
        self.assert_originals()

    def test_preview_change_or_missing_token_does_not_call_provider(self):
        old = self.preview()
        (self.vault / "papers" / self.name).write_bytes(self.expected["papers/" + self.name] + b"changed")
        with store.changing(self.vault), self.assertRaises(store.Busy):
            store.trash(self.vault, app.load_queue(self.vault), self.save, name=self.name, token=old["token"])
        with store.changing(self.vault), self.assertRaises(store.Busy):
            store.trash(self.vault, app.load_queue(self.vault), self.save, name=self.name)
        self.assertEqual(0, self.bin.calls)

    def test_pure_queue_removal_has_no_system_files_and_never_auto_restores(self):
        result = self.trash(queue_item=self.items[1]["id"])
        self.assertTrue(result["queue_only"])
        self.assertEqual(0, self.bin.calls)
        self.assertEqual([], self.reconcile())
        self.assertEqual([self.row], app.load_queue(self.vault))

    def test_versioned_pdf_and_metadata_share_base_identity(self):
        self.items[0]["label"] = PAPER + "v2.pdf"
        old, new = "processed/a1b2c3d4e5-" + PAPER + ".pdf", "processed/a1b2c3d4e5-" + PAPER + "v2.pdf"
        (self.vault / old).rename(self.vault / new)
        self.expected[new] = self.expected.pop(old)
        app.save_queue(self.vault, self.items)
        versioned = self.vault / "papers" / (PAPER + "v2-legacy.md")
        versioned.write_text("# Legacy\n\n- arXiv: https://arxiv.org/abs/" + PAPER + "v2", encoding="utf-8")
        result = self.trash(queue_item=self.row["id"])
        self.assertEqual(PAPER, result["paper_id"])
        self.assertFalse(versioned.exists())
        with self.assertRaises(store.Gone):
            app.add_queue_item(self.vault, "link", PAPER + "v3")
        self.bin.restore()
        self.reconcile()
        self.assert_originals()

    def test_native_restore_invalidates_old_reader_epoch(self):
        old = store.lifecycle_epoch(self.vault, PAPER)
        self.trash()
        deleted = store.lifecycle_epoch(self.vault, PAPER)
        with self.assertRaises(store.Gone):
            store.check_lifecycle(self.vault, PAPER, old)
        self.bin.restore()
        self.reconcile()
        current = store.lifecycle_epoch(self.vault, PAPER)
        self.assertNotEqual(deleted, current)
        for stale in (old, deleted):
            with self.assertRaises(store.Gone):
                store.check_lifecycle(self.vault, PAPER, stale)
        store.check_lifecycle(self.vault, PAPER, current)

    def test_partial_restore_and_changed_files_are_not_overwritten_or_claimed_complete(self):
        self.trash()
        note = str(self.vault / "papers" / self.name)
        self.bin.restore(only={note})
        self.assertEqual([], self.reconcile())
        self.assertIn(PAPER, store.deleted_ids(self.vault))
        self.bin.restore()
        Path(note).write_bytes(b"User edited the restored note")
        self.assertEqual([], self.reconcile())
        self.assertEqual(b"User edited the restored note", Path(note).read_bytes())
        self.assertTrue(store.attention(self.vault)["pending"])

    def test_unrelated_citation_traversal_and_corrupt_queue_are_rejected_before_recycling(self):
        for name in (self.unrelated.name, "../config.json", "C:secret.md"):
            with self.assertRaises(ValueError):
                self.preview(name=name)
        self.items[0]["stored"] = "../external-original.pdf"
        app.save_queue(self.vault, self.items)
        with self.assertRaises(ValueError):
            self.preview()
        self.assertEqual(0, self.bin.calls)
        self.assert_originals()

    def test_receipt_path_tampering_fails_closed(self):
        self.trash()
        path = self.vault / store.RECEIPTS
        data = json.loads(path.read_text(encoding="utf-8"))
        data["papers"][PAPER]["paths"] = ["config.json"]
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.reconcile()
        self.assertTrue(self.external.is_file())

    def test_queue_update_failure_can_finish_without_recycling_files_twice(self):
        with patch.object(app, "save_queue", side_effect=OSError("simulated queue write failure")):
            result = self.trash()
        self.assertTrue(result["metadata_pending"])
        self.assertTrue(result["partial"])
        calls = self.bin.calls
        final = self.trash(receipt_id=result["id"])
        self.assertTrue(final["ok"])
        self.assertEqual(calls, self.bin.calls)
        self.bin.restore()
        self.reconcile()
        self.assert_originals()

    def test_old_preview_cannot_delete_a_restored_generation(self):
        old = self.preview()
        self.trash()
        self.bin.restore()
        self.reconcile()
        calls = self.bin.calls
        with store.changing(self.vault), self.assertRaises(store.Busy):
            store.trash(self.vault, app.load_queue(self.vault), self.save, name=self.name, token=old["token"])
        self.assertEqual(calls, self.bin.calls)
        self.assert_originals()

    def test_shared_queue_identifier_is_rejected_before_any_os_call(self):
        self.items[1]["id"] = self.items[0]["id"]
        app.save_queue(self.vault, self.items)
        with self.assertRaises(ValueError):
            self.preview()
        self.assertEqual(0, self.bin.calls)
        self.assert_originals()

    def test_failed_receipt_creation_prevents_any_os_call(self):
        preview = self.preview()
        with patch.object(store, "_save_native", side_effect=OSError("read only")), store.changing(self.vault), self.assertRaises(OSError):
            store.trash(self.vault, self.items, self.save, name=self.name, token=preview["token"])
        self.assertEqual(0, self.bin.calls)
        self.assert_originals()

    def test_post_recycle_metadata_failure_keeps_tombstone_and_is_recoverable(self):
        original = store._save_native
        calls = 0
        def fail_second(vault, data):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated metadata failure")
            original(vault, data)
        with patch.object(store, "_save_native", side_effect=fail_second):
            result = self.trash()
        self.assertTrue(result["partial"] and result["receipt_pending"] and result["restore_required"])
        self.assertGreater(result["completed"], 0)
        self.assertIn(PAPER, store.deleted_ids(self.vault))
        self.assertNotIn(self.row, app.active_queue(self.vault))
        self.bin.restore()
        self.reconcile()
        self.assert_originals()

    def test_final_receipt_write_failure_remains_discoverable_and_retries_metadata_only(self):
        original = store._save_native
        calls = 0
        def fail_final(vault, data):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("simulated final status write failure")
            original(vault, data)
        with patch.object(store, "_save_native", side_effect=fail_final):
            result = self.trash()
        self.assertTrue(result["metadata_pending"])
        self.assertFalse(result["restore_required"])
        self.assertEqual("pending", store.attention(self.vault)["pending"][0]["kind"])
        before = self.bin.calls
        self.assertTrue(self.trash(receipt_id=result["id"])["ok"])
        self.assertEqual(before, self.bin.calls)
        self.bin.restore()
        self.reconcile()
        self.assert_originals()

    def test_partial_receipt_write_failure_preserves_exact_results_and_original_queue(self):
        original = store._save_native
        calls = 0
        self.bin.fail_after = 1
        def fail_partial(vault, data):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated partial status write failure")
            original(vault, data)
        with patch.object(store, "_save_native", side_effect=fail_partial):
            result = self.trash()
        self.assertTrue(result["partial"] and result["restore_required"])
        self.assertEqual(1, result["completed"])
        self.assertGreater(result["remaining"], 0)
        self.assertEqual(self.items, app.load_queue(self.vault))
        self.bin.restore()
        self.reconcile()
        self.assert_originals()

    def test_reading_and_generation_gate_block_changes_without_serializing_readers(self):
        with store.reading(self.vault), store.reading(self.vault), self.assertRaises(store.Busy):
            self.trash()
        self.items[0]["status"] = "processing"
        app.save_queue(self.vault, self.items)
        with self.assertRaises(store.Busy):
            self.preview()
        self.assert_originals()

    def test_paused_inbox_generation_blocks_native_delete_and_then_is_recyclable(self):
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
        self.assertEqual([], errors)
        self.assertTrue(self.trash()["ok"])

    def test_tombstoned_inbox_is_rejected_before_network_or_model(self):
        self.trash()
        src = self.vault / "inbox" / "paper.txt"
        src.write_text(PAPER + "v2", encoding="utf-8")
        with patch.object(app, "fetch_arxiv") as fetch, patch.object(app, "llm_chat") as chat:
            with self.assertRaises(store.Gone):
                app.process_file(self.vault, self.vault / "inbox", self.vault / "processed", src)
            fetch.assert_not_called()
            chat.assert_not_called()
        self.assertTrue(src.exists())

    def test_explicit_reimport_changes_epoch_but_cannot_activate_another_paper(self):
        result = self.trash()
        old = store.lifecycle_epoch(self.vault, PAPER)
        with store.changing(self.vault), store.reactivating(self.vault, PAPER, result["id"]):
            with self.assertRaises(ValueError):
                store.ensure_active(self.vault, OTHER)
            store.ensure_active(self.vault, PAPER)
        current = store.lifecycle_epoch(self.vault, PAPER)
        self.assertNotEqual(old, current)
        with self.assertRaises(store.Gone):
            store.check_lifecycle(self.vault, PAPER, old)
        store.check_lifecycle(self.vault, PAPER, current)

    def test_legacy_record_is_only_restored_explicitly(self):
        plan = self.preview()
        entry = uuid.uuid4().hex
        root = self.vault / store.ROOT / entry
        data = {"entry":entry,"paper_id":PAPER,"title":"Legacy fixture","date":"2026-10-23T00:00:00+00:00",
                "state":"trashed","paths":plan["paths"],"queue":[self.row]}
        store._json(root / "manifest.json",data)
        store._json(self.vault / store.ROOT / "index.json",{PAPER:entry})
        for relative in plan["paths"]:
            target = root / "files" / relative
            target.parent.mkdir(parents=True,exist_ok=True)
            (self.vault / relative).rename(target)
        self.save([self.items[1]])
        self.assertEqual([],self.reconcile())
        self.assertTrue(store.attention(self.vault)["legacy"])
        self.assertFalse((self.vault / "papers" / self.name).exists())
        with store.changing(self.vault):
            store.restore(self.vault,entry,app.load_queue(self.vault),self.save)
            store.activate_legacy_restored(self.vault,PAPER)
        self.assert_originals()
        self.assertEqual([],store.attention(self.vault)["legacy"])

    def test_non_recyclable_transfer_flag_is_rejected(self):
        for flags in (0, 1, 0x100):
            with self.assertRaises(OSError):
                native.require_recyclable(flags)
        native.require_recyclable(native.RECYCLE_TRANSFER)


if __name__ == "__main__":
    unittest.main()
