"""Offline regression tests. Fixtures stay in qa/test-runs for inspection."""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import lazyscholar as app  # noqa: E402 — direct script adds the repository first.

RUN = ROOT / "qa" / "test-runs" / uuid.uuid4().hex[:10]


def new_vault(name: str) -> Path:
    vault = RUN / name
    app._ensure_vault(vault)
    return vault


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.vault = new_vault(self._testMethodName)
        self.prefs = self.vault / "preferences.json"
        self.env = patch.dict(os.environ, {"LAZYSCHOLAR_PREFERENCES": str(self.prefs)})
        self.env.start()
        app._shutting_down.clear()

    def tearDown(self):
        app._shutting_down.clear()
        self.env.stop()

    def test_duplicate_and_invalid_inputs(self):
        first = app.add_queue_item(self.vault, "link", "https://arxiv.org/abs/2510.16720")
        with self.assertRaisesRegex(ValueError, "已在队列"):
            app.add_queue_item(self.vault, "link", "2510.16720v2")
        with self.assertRaises(ValueError):
            app.add_queue_item(self.vault, "link", "not a paper")
        self.assertEqual([first], app.load_queue(self.vault))

    def test_pdf_validation_and_original_preserved(self):
        source = self.vault / "2510.16720 中文论文.pdf"
        source.write_bytes(b"%PDF-1.7\nfixture")
        item = app.add_queue_item(self.vault, "pdf", str(source))
        self.assertEqual(source.read_bytes(), (self.vault / "queue_files" / item["stored"]).read_bytes())
        self.assertTrue(source.exists())
        bad = self.vault / "2510.00001.pdf"
        bad.write_text("not PDF", encoding="utf-8")
        with self.assertRaises(ValueError):
            app.add_queue_item(self.vault, "pdf", str(bad))
        self.assertEqual(1, len(app.load_queue(self.vault)))

    def test_upload_version_conflict_preserves_cached_pdf_and_marks(self):
        paper_id = "2510.00009"
        source = self.vault / f"{paper_id}.pdf"
        source.write_bytes(b"%PDF-1.7\nnew version")
        cached = self.vault / "papers_pdf" / source.name
        cached.parent.mkdir(exist_ok=True)
        original = b"%PDF-1.7\noriginal version"
        cached.write_bytes(original)
        marks = [{"page": 1, "x": .1, "y": .1, "w": .2, "h": .02}]
        app.save_highlights(self.vault, paper_id, {"highlights": marks})
        with self.assertRaisesRegex(ValueError, "不同版本"):
            app.add_queue_item(self.vault, "pdf", str(source))
        self.assertEqual(original, cached.read_bytes())
        self.assertEqual(marks, app.load_highlights(self.vault, paper_id)["highlights"])
        self.assertEqual([], app.load_queue(self.vault))
        source.write_bytes(original)
        item = app.add_queue_item(self.vault, "pdf", str(source))
        # Also protect already-persisted queues created by an older app version.
        (self.vault / "queue_files" / item["stored"]).write_bytes(b"%PDF-1.7\nchanged upload")
        with self.assertRaisesRegex(ValueError, "缓存版本不同"):
            app.get_pdf_path(self.vault, paper_id)
        self.assertEqual(original, cached.read_bytes())
        self.assertEqual(marks, app.load_highlights(self.vault, paper_id)["highlights"])

    def test_pending_upload_reads_without_metadata_or_model(self):
        source = self.vault / "2510.00003.pdf"
        source.write_bytes(b"%PDF-1.7\nlocal-only fixture")
        item = app.add_queue_item(self.vault, "pdf", str(source))
        with patch.object(app, "fetch_arxiv", side_effect=AssertionError("metadata used")), \
                patch.object(app, "llm_chat", side_effect=AssertionError("model used")), \
                patch.object(app.urllib.request, "urlopen", side_effect=AssertionError("network used")):
            info = app.queue_reading_info(self.vault, item["id"])
            self.assertEqual("pdf", info["view"])
            self.assertIsNone(info["note"])
            self.assertEqual(source.read_bytes(), app.get_pdf_path(self.vault, info["arxiv"]).read_bytes())
        self.assertEqual("pending", app.load_queue(self.vault)[0]["status"])
        self.assertEqual([], list((self.vault / "papers").glob("*.md")))
        self.assertTrue((self.vault / "queue_files" / item["stored"]).is_file())

    def test_failed_generation_does_not_block_reading_or_retry(self):
        source = self.vault / "2510.00004.pdf"
        source.write_bytes(b"%PDF-1.7\nkeep this upload")
        item = app.add_queue_item(self.vault, "pdf", str(source))
        with patch.object(app, "llama_health", return_value=True), \
                patch.object(app, "fetch_arxiv", side_effect=OSError("metadata offline")):
            result = app.process_queue(self.vault)
        self.assertEqual(1, result["failed"])
        with patch.object(app.urllib.request, "urlopen", side_effect=AssertionError("network used")):
            self.assertEqual(source.read_bytes(), app.get_pdf_path(self.vault, "2510.00004").read_bytes())
        self.assertIsNone(app.queue_reading_info(self.vault, item["id"])["note"])
        app.retry_queue_item(self.vault, item["id"])
        self.assertEqual("pending", app.load_queue(self.vault)[0]["status"])
        self.assertTrue(source.is_file())

    def test_queue_reader_handles_links_and_missing_notes(self):
        item = app.add_queue_item(self.vault, "link", "2510.00005")
        app.update_queue_item(self.vault, item["id"], note="missing.md", status="done")
        info = app.queue_reading_info(self.vault, item["id"])
        self.assertEqual("paper", info["view"])
        self.assertIsNone(info["note"])
        with self.assertRaises(ValueError):
            app.queue_reading_info(self.vault, "missing")

    def test_desktop_reader_bridge_and_exit_lifecycle(self):
        import app_shell

        class Hook(list):
            def __iadd__(self, callback):
                self.append(callback)
                return self

        windows = []
        fake_webview = MagicMock()

        def create_window(*args, **kwargs):
            window = MagicMock()
            window.events.closed = Hook()
            window.events.closing = Hook()
            window.events.moved = Hook()
            window.reader_options = kwargs

            def destroy():
                if any(callback() is False for callback in list(window.events.closing)):
                    return
                for callback in list(window.events.closed):
                    callback()

            window.destroy.side_effect = destroy
            windows.append(window)
            return window

        def start(**kwargs):
            panel = windows[0]
            bridge = panel.reader_options["js_api"]
            params = {"reader": "2510.16720", "view": "pdf"}
            self.assertTrue(bridge.open_reader(params))
            first = windows[-1]
            self.assertTrue(first.reader_options["resizable"])
            self.assertEqual(1100, first.reader_options["width"])
            self.assertEqual(850, first.reader_options["height"])
            self.assertIn("reader=2510.16720", fake_webview.create_window.call_args.args[1])
            self.assertFalse(hasattr(first.reader_options["js_api"], "pick_pdf"))
            first.reader_options["js_api"].close_reader()
            first.destroy.assert_called_once()
            bridge.open_reader(params)
            second = windows[-1]
            controller = kwargs["func"].__self__
            controller._do_quit()
            second.destroy.assert_called_once()
            first.destroy.assert_called_once()  # Already-closed windows were unregistered.

        fake_webview.create_window.side_effect = create_window
        fake_webview.start.side_effect = start
        with patch.dict(sys.modules, {"webview": fake_webview}), \
                patch.object(app.threading, "Thread"), \
                patch.object(app_shell, "acquire_single_instance", return_value=True), \
                patch.object(app_shell, "work_area", return_value=(0, 0, 1920, 1080)), \
                patch.object(app_shell, "dpi_scale", return_value=1.0), \
                patch.object(app_shell, "WIN", True), \
                patch.object(app_shell, "BallWindow"), \
                patch.object(app_shell, "build_tray", return_value=None), \
                patch.object(app_shell, "start_wake_listener", return_value=None), \
                patch.object(app, "shutdown_model"):
            self.assertEqual(0, app.run_app(self.vault, 30))

    def test_reader_window_url_only_targets_local_app(self):
        params = {"reader": "2510.16720", "view": "pdf", "kind": "papers", "name": "中文导读.md"}
        url = urllib.parse.urlsplit(app.reader_window_url(params))
        self.assertEqual("127.0.0.1", url.hostname)
        self.assertEqual("/app", url.path)
        self.assertEqual([params["name"]], urllib.parse.parse_qs(url.query)["name"])
        for bad in ({"reader": "https://evil.example"}, {"reader": "2510.16720", "url": "evil"},
                    {"reader": "2510.16720", "view": "javascript:alert(1)"},
                    {"reader": "2510.16720", "queue": "../file"},
                    {"reader": "2510.16720", "kind": "papers", "name": "../secret.md"},
                    {"reader": "2510.16720", "kind": "papers", "name": "..\\secret.md"}):
            with self.assertRaises(ValueError):
                app.reader_window_url(bad)

    def test_atomic_write_keeps_previous_json_on_failure(self):
        first = app.add_queue_item(self.vault, "link", "2510.16720")
        before = (self.vault / "queue.json").read_bytes()
        with patch.object(app.os, "replace", side_effect=OSError("disk unavailable")), self.assertRaises(OSError):
            app.save_queue(self.vault, [])
        self.assertEqual(before, (self.vault / "queue.json").read_bytes())
        self.assertEqual([first], app.load_queue(self.vault))
        self.assertEqual([], list(self.vault.glob(".*.tmp")))

    def test_corrupt_queue_is_not_silently_overwritten(self):
        path = self.vault / "queue.json"
        for content in ("{broken", "{}", "[{}]"):
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(ValueError):
                app.add_queue_item(self.vault, "link", "2510.16720")
            self.assertEqual(content, path.read_text(encoding="utf-8"))

    def test_recover_retry_and_protect_processing_item(self):
        item = app.add_queue_item(self.vault, "link", "2510.16720")
        app.update_queue_item(self.vault, item["id"], status="processing")
        with self.assertRaises(ValueError):
            app.delete_queue_item(self.vault, item["id"])
        self.assertEqual(1, app.recover_queue(self.vault))
        self.assertEqual("pending", app.load_queue(self.vault)[0]["status"])
        app.update_queue_item(self.vault, item["id"], status="failed", error="network unavailable")
        app.retry_queue_item(self.vault, item["id"])
        self.assertIsNone(app.load_queue(self.vault)[0]["error"])
        with self.assertRaises(ValueError):
            app.retry_queue_item(self.vault, item["id"])

    def test_concurrent_add_delete_and_second_batch(self):
        first = app.add_queue_item(self.vault, "link", "2510.16720")
        removed = app.add_queue_item(self.vault, "link", "2510.00002")
        entered, release = threading.Event(), threading.Event()
        results = []

        def process(vault, item):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test synchronisation timed out")
            return "fixture.md"

        with patch.object(app, "llama_health", return_value=True), patch.object(app, "_process_queue_item", side_effect=process):
            worker = threading.Thread(target=lambda: results.append(app.process_queue(self.vault)))
            worker.start()
            try:
                self.assertTrue(entered.wait(10))
                self.assertFalse(app.process_queue(self.vault)["ok"])
                app.delete_queue_item(self.vault, removed["id"])
                added = app.add_queue_item(self.vault, "link", "2510.00003")
            finally:
                release.set()
                worker.join(10)
            self.assertFalse(worker.is_alive())
        rows = {i["id"]: i for i in app.load_queue(self.vault)}
        self.assertEqual({first["id"], added["id"]}, set(rows))
        self.assertEqual("done", rows[first["id"]]["status"])
        self.assertEqual("pending", rows[added["id"]]["status"])
        self.assertEqual(1, results[0]["processed"])

    def test_model_cleanup_after_processing_failure(self):
        app.add_queue_item(self.vault, "link", "2510.16720")
        with patch.object(app, "llama_health", return_value=False), patch.object(app, "start_server", return_value=True), patch.object(app, "stop_server") as stop, patch.object(app, "_process_queue_item", side_effect=RuntimeError("inference failed")):
            result = app.process_queue(self.vault)
        self.assertEqual(1, result["failed"])
        self.assertEqual("failed", app.load_queue(self.vault)[0]["status"])
        stop.assert_called_once_with(self.vault)
        self.assertFalse(app._batch["running"])

    def test_model_cleanup_after_startup_failure(self):
        app.add_queue_item(self.vault, "link", "2510.16720")
        with patch.object(app, "llama_health", return_value=False), patch.object(app, "start_server", return_value=False), patch.object(app, "stop_server") as stop:
            self.assertFalse(app.process_queue(self.vault)["ok"])
        stop.assert_called_once()
        self.assertFalse(app._batch["running"])
        self.assertEqual("pending", app.load_queue(self.vault)[0]["status"])
        self.assertTrue(app._batch_lock.acquire(blocking=False))
        app._batch_lock.release()

    def test_shutdown_cleans_a_launch_already_inside_popen(self):
        entered, release = threading.Event(), threading.Event()
        process = MagicMock()
        process.poll.return_value = None
        results = []

        def spawn(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("launch barrier timed out")
            return process

        with patch.object(app, "llama_health", return_value=False), patch.object(app.subprocess, "Popen", side_effect=spawn) as popen:
            starter = threading.Thread(target=lambda: results.append(app.start_server(self.vault, {"port": 8080, "model_path": "fixture.gguf"})))
            closer = threading.Thread(target=lambda: app.shutdown_model(self.vault))
            starter.start()
            try:
                self.assertTrue(entered.wait(10))
                closer.start()
                self.assertTrue(app._shutting_down.wait(10))
            finally:
                release.set()
                starter.join(10)
                if closer.ident is not None:
                    closer.join(10)
            self.assertFalse(starter.is_alive())
            self.assertFalse(closer.is_alive())
            self.assertEqual([False], results)
            process.terminate.assert_called_once()
            self.assertIsNone(app._server["proc"])
            self.assertFalse(app.start_server(self.vault, {"port": 8080, "model_path": "fixture.gguf"}))
            popen.assert_called_once()

    def test_storage_switch_does_not_move_old_files(self):
        note = self.vault / "papers" / "old.md"
        note.write_text("# Keep me", encoding="utf-8")
        selected = self.vault / "新知识库 with spaces"
        selected.mkdir()
        state = app.select_vault(self.vault, str(selected))
        self.assertTrue(state["restart_required"])
        self.assertEqual(str(self.vault), state["active"])
        self.assertEqual("# Keep me", note.read_text(encoding="utf-8"))
        self.assertEqual([], list(selected.iterdir()))
        with patch.dict(os.environ, {"LAZYSCHOLAR_VAULT": ""}):
            self.assertEqual(str(selected.resolve()), app._default_vault())
        with patch.dict(os.environ, {"LAZYSCHOLAR_VAULT": str(self.vault)}):
            self.assertEqual(str(self.vault), app._default_vault())

    def test_invalid_storage_does_not_replace_selection(self):
        app.select_vault(self.vault, str(self.vault))
        before = self.prefs.read_bytes()
        for value in ("", "relative/path", str(self.vault / "missing")):
            with self.assertRaises(ValueError):
                app.select_vault(self.vault, value)
        self.assertEqual(before, self.prefs.read_bytes())

    def test_config_validation_and_positions(self):
        cfg = app.save_config(self.vault, {"port": 8081, "schedule": "09:15", "panel_y": 88, "ball_y": 99})
        self.assertEqual(88, cfg["panel_y"])
        self.assertEqual(99, app.load_config(self.vault)["ball_y"])
        for update in ({"port": 0}, {"ctx": "no"}, {"schedule": "25:00"}):
            with self.assertRaises(ValueError):
                app.save_config(self.vault, update)
        self.assertEqual(cfg, app.load_config(self.vault))

    def test_highlight_roundtrip_and_validation(self):
        h = {"page": 1, "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.04}
        app.save_highlights(self.vault, "2510.16720", {"highlights": [h]})
        self.assertEqual([h], app.load_highlights(self.vault, "2510.16720")["highlights"])
        with self.assertRaises(ValueError):
            app.save_highlights(self.vault, "2510.16720", {"highlights": [{**h, "x": -1}]})
        self.assertEqual([h], app.load_highlights(self.vault, "2510.16720")["highlights"])

    def test_empty_generation_is_not_a_successful_note(self):
        meta = {"title": "Fixture", "authors": ["Test"], "summary": "Abstract", "url": "https://arxiv.org/abs/2510.16720"}
        with patch.object(app, "llm_chat", return_value=""), self.assertRaises(ValueError):
            app.write_paper_note(self.vault, meta, "2510.16720")
        self.assertEqual([], list((self.vault / "papers").iterdir()))

    def test_corrupt_config_and_highlights_are_preserved(self):
        config = self.vault / "config.json"
        config.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            app.save_config(self.vault, {"port": 8081})
        self.assertEqual("{broken", config.read_text(encoding="utf-8"))
        folder = self.vault / "highlights"
        folder.mkdir()
        path = folder / "2510.16720.json"
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            app.load_highlights(self.vault, "2510.16720")
        self.assertEqual("{broken", path.read_text(encoding="utf-8"))
        # Assertions above prove preservation; retain valid artifacts for IDE scans.
        config.write_text("{}", encoding="utf-8")
        path.write_text('{"highlights": []}', encoding="utf-8")

    def test_arxiv_rejects_dtd_and_oversized_responses(self):
        for raw, message in ((b'<!DOCTYPE a [<!ENTITY x "boom">]><feed/>', "DTDs"),
                             (b"x" * (2 * 1024 * 1024 + 1), "too large")):
            with patch.object(app.urllib.request, "urlopen", return_value=io.BytesIO(raw)), self.assertRaisesRegex(ValueError, message):
                app.fetch_arxiv("2510.16720", attempts=1)

    def test_uploaded_pdf_is_cached_for_offline_reading(self):
        from pdf_fixture import paper_fixture

        source = self.vault / "2510.16720.pdf"
        source.write_bytes(paper_fixture())
        item = app.add_queue_item(self.vault, "pdf", str(source))
        with patch.object(app, "write_full_text_note", return_value=self.vault / "papers" / "fixture.md"):
            app._process_queue_item(self.vault, item)
        with patch.object(app.urllib.request, "urlopen", side_effect=AssertionError("Must not download an uploaded PDF")):
            self.assertEqual(source.read_bytes(), app.get_pdf_path(self.vault, "2510.16720").read_bytes())
        self.assertTrue(source.exists())

    def test_all_tabs_align_at_each_scale_and_clamp(self):
        from app_shell import AppController
        for scale, maximum in ((1, 900), (1.5, 1100), (2, 700)):
            ctl = AppController(None, (0, 0), scale=scale, max_h=maximum)
            self.assertEqual({min(round(642 * scale), maximum)}, {ctl._tab_h(k) for k in ctl.TAB_HEIGHTS})


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vault = new_vault("http")
        (cls.vault / "papers" / "fixture.md").write_text("# A real note\n\n## Summary\n\n**Readable**\n\narXiv: 2510.16720", encoding="utf-8")
        (cls.vault / "papers_pdf").mkdir()
        from pdf_fixture import paper_fixture
        (cls.vault / "papers_pdf" / "2510.16720.pdf").write_bytes(paper_fixture())
        from html_fixture import seed_html
        cls.html_doc = seed_html(cls.vault)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        cls.port = sock.getsockname()[1]
        sock.close()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        env = dict(os.environ, LAZYSCHOLAR_UI_PORT=str(cls.port), LAZYSCHOLAR_PREFERENCES=str(cls.vault / "preferences.json"))
        cls.log = (cls.vault / "server-output.log").open("w", encoding="utf-8")
        cls.proc = subprocess.Popen([sys.executable, "-B", str(ROOT / "lazyscholar.py"), "--ui", "--vault", str(cls.vault)], env=env, stdout=cls.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                cls.opener.open(cls.base + "/app", timeout=1).close()
                return
            except (OSError, urllib.error.URLError):
                if cls.proc.poll() is not None:
                    break
                threading.Event().wait(0.1)
        cls.tearDownClass()
        raise RuntimeError(f"HTTP fixture did not start; see {cls.vault}")

    @classmethod
    def tearDownClass(cls):
        if cls.proc.poll() is None:
            cls.proc.terminate()
            cls.proc.wait(timeout=10)
        cls.log.close()

    def request(self, path, body=None, *, with_revision=True) -> tuple[int, Any]:
        if (with_revision and isinstance(body, dict) and "revision" not in body
                and path.split("?", 1)[0] in ("/api/highlights", "/api/reading-highlights")):
            status, current = self.request(path, with_revision=False)
            if status == 200:
                body = {**body, "revision": current["revision"]}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json"})
        try:
            response = self.opener.open(req, timeout=10)
        except urllib.error.HTTPError as e:
            response = e
        with response:
            raw = response.read()
            result = json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw
            status = response.getcode()
            if not isinstance(status, int):
                raise AssertionError("HTTP status is missing")
            return status, result

    def test_notes_and_html_contract(self):
        status, state = self.request("/api/notes")
        self.assertEqual(200, status)
        self.assertEqual("fixture.md", state["papers"][0]["name"])
        status, note = self.request("/api/note?kind=papers&name=fixture.md")
        self.assertEqual(200, status)
        self.assertIn("<strong>Readable</strong>", note["html"])
        self.assertEqual("2510.16720", note["arxiv"])

    def test_pdf_is_binary_not_json(self):
        status, data = self.request("/api/pdf?id=2510.16720")
        self.assertEqual(200, status)
        self.assertTrue(data.startswith(b"%PDF"))

    def test_reflow_text_images_and_annotations_contract(self):
        status, page = self.request("/api/paper?id=2510.16720&page=1")
        self.assertEqual(200, status)
        self.assertEqual("html", page["source_kind"])
        self.assertEqual(58, page["math_count"])
        block = next(r for b in page["blocks"] for r in b["runs"] if r["text"].startswith("Left paragraph"))
        image = next(iter(self.html_doc["images"]))
        self.assertTrue(self.request("/api/paper/html-image?id=2510.16720&name=" + image)[1].startswith(b"\x89PNG"))
        self.assertGreater(self.request("/api/paper/anchor?id=2510.16720&name=ls-source-S2")[1]["page"], 1)
        h = {"block": block["id"], "start": 0, "end": 4, "quote": "Left"}
        self.assertEqual(200, self.request("/api/reading-highlights?id=2510.16720", {"source": page["source"], "highlights": [h]})[0])
        self.assertEqual([h], self.request("/api/reading-highlights?id=2510.16720")[1]["highlights"])
        self.assertEqual(400, self.request("/api/paper?id=2510.16720&page=0")[0])

    def test_highlight_contract_persists_and_clears(self):
        h = {"page": 1, "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.04}
        self.assertEqual(200, self.request("/api/highlights?id=2510.16720", {"highlights": [h]})[0])
        self.assertEqual([h], self.request("/api/highlights?id=2510.16720")[1]["highlights"])
        self.request("/api/highlights?id=2510.16720", {"highlights": []})
        self.assertEqual([], self.request("/api/highlights?id=2510.16720")[1]["highlights"])

    def test_local_origin_and_proxy_validation(self):
        for headers in ({"Host": "evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/config", headers=headers)
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.opener.open(req, timeout=5)
            self.assertEqual(403, error.exception.code)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/config", data=b"{}", headers={"Origin": "https://evil.example", "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.opener.open(req, timeout=5)
        self.assertEqual(403, error.exception.code)
        self.assertEqual(400, self.request("/api/config", {"html_proxy": "file:///bad"})[0])

    def test_bad_config_and_json_return_visible_errors(self):
        status, data = self.request("/api/config", {"port": -1})
        self.assertEqual(400, status)
        self.assertFalse(data["ok"])
        status, data = self.request("/api/config", ["not", "an", "object"])
        self.assertEqual(400, status)
        self.assertFalse(data["ok"])

    def test_queue_add_and_duplicate_contract(self):
        status, result = self.request("/api/queue", {"kind": "link", "value": "2510.12345"})
        self.assertEqual(200, status)
        self.assertTrue(result["ok"])
        self.assertEqual(400, self.request("/api/queue", {"kind": "link", "value": "2510.12345"})[0])
        self.assertIsInstance(self.request("/api/queue")[1], list)
        self.request("/api/queue/delete", {"id": result["item"]["id"]})

    def test_pending_pdf_reader_api_needs_no_note(self):
        source = self.vault / "2510.00006.pdf"
        original = b"%PDF-1.7\nHTTP pending upload"
        source.write_bytes(original)
        status, result = self.request("/api/queue", {"kind": "pdf", "value": str(source)})
        self.assertEqual(200, status)
        item_id = result["item"]["id"]
        try:
            status, info = self.request("/api/queue/reading?id=" + item_id)
            self.assertEqual(200, status)
            self.assertEqual("2510.00006", info["arxiv"])
            self.assertEqual("pdf", info["view"])
            self.assertIsNone(info["note"])
            self.assertEqual((200, original), self.request("/api/pdf?id=2510.00006"))
            status, items = self.request("/api/queue")
            self.assertEqual("pending", next(i for i in items if i["id"] == item_id)["status"])
        finally:
            self.request("/api/queue/delete", {"id": item_id})
        self.assertEqual(400, self.request("/api/queue/reading?id=missing")[0])

    def _check_annotation_write_race(self, url, base, first, second):
        status, initial = self.request(url)
        self.assertEqual(200, status)
        self.assertEqual(409, self.request(url, {**base, "highlights": []}, with_revision=False)[0])
        barrier = threading.Barrier(2)

        def save(rows):
            barrier.wait(timeout=5)
            return self.request(url, {**base, "revision": initial["revision"], "highlights": rows}, with_revision=False)

        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = [executor.submit(save, rows) for rows in ([first], [second])]
            results = [future.result(timeout=10) for future in pending]
        self.assertEqual([200, 409], sorted(status for status, _ in results))
        current = self.request(url)[1]
        self.assertEqual(1, len(current["highlights"]))
        self.assertNotEqual(initial["revision"], current["revision"])
        missing = second if first in current["highlights"] else first
        status, saved = self.request(url, {**base, "revision": current["revision"],
                                          "highlights": current["highlights"] + [missing]}, with_revision=False)
        self.assertEqual(200, status)
        self.assertCountEqual([first, second], saved["highlights"])
        before = self.request(url)[1]
        self.assertEqual(409, self.request(url, {**base, "revision": initial["revision"], "highlights": []},
                                          with_revision=False)[0])
        self.assertEqual(before, self.request(url)[1])

    def test_pdf_annotation_revision_is_required_and_atomic(self):
        self._check_annotation_write_race("/api/highlights?id=2510.00014", {},
                                         {"page": 1, "x": .1, "y": .1, "w": .1, "h": .02},
                                         {"page": 1, "x": .4, "y": .1, "w": .1, "h": .02})

    def test_html_annotation_revision_is_required_and_atomic(self):
        from html_fixture import seed_html

        paper_id = "2510.00014"
        document = seed_html(self.vault, paper_id)
        run = document["runs"][0]
        first = {"block": run["id"], "start": 0, "end": 4, "quote": run["text"][:4]}
        second = {"block": run["id"], "start": 10, "end": 15, "quote": run["text"][10:15]}
        self._check_annotation_write_race("/api/reading-highlights?id=" + paper_id,
                                         {"source": document["source"]}, first, second)

    def test_storage_state_contract(self):
        status, data = self.request("/api/storage")
        self.assertEqual(200, status)
        self.assertEqual(str(self.vault), data["active"])
        self.assertFalse(data["restart_required"])


if __name__ == "__main__":
    print(f"Test artifacts: {RUN}", flush=True)
    unittest.main(verbosity=2)
