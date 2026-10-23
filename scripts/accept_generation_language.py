"""Opt-in real-model acceptance: settings UI -> persisted language -> queued guide.

Uses isolated fixture data and an owned model server; optional --exe tests a package.
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import test_app_backend as backend
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, help="Test this packaged EXE instead of the Python HTTP app")
    args = parser.parse_args()
    exe = args.exe.resolve() if args.exe else None
    if exe is not None and not exe.is_file():
        raise ValueError("Packaged EXE does not exist")
    app = backend.app
    run = backend.new_vault("real-generation-language")
    cfg = app.load_config(Path(app._default_vault()))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        cfg["port"] = sock.getsockname()[1]
    cfg["ctx"] = 16384
    report = {"exe": str(exe) if exe else None, "run": str(run), "checks": []}
    fixture_started = False
    model_process = None
    try:
        if not app.start_server(run, cfg):
            raise RuntimeError("Isolated model server did not start")
        model_process = app._server["proc"]
        if exe is not None:
            original_popen = subprocess.Popen
            def launch_packaged(command, **kwargs):
                return original_popen([str(exe), "--ui", "--vault", str(backend.ApiTests.vault)], **kwargs)
            with patch.object(backend.subprocess, "Popen", side_effect=launch_packaged):
                backend.ApiTests.setUpClass()
        else:
            backend.ApiTests.setUpClass()
        fixture_started = True
        api = backend.ApiTests()
        status, _result = api.request("/api/config", {"port": cfg["port"], "ctx": cfg["ctx"], "lang": "zh"})
        if status != 200:
            raise AssertionError("Could not configure isolated model endpoint")
        existing = backend.ApiTests.vault / "papers" / "fixture.md"
        original_note = existing.read_bytes()
        report["gpu"] = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,utilization.gpu", "--format=csv"],
            capture_output=True, text=True, check=False).stdout.strip()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=pw.chromium.executable_path)
            try:
                page = browser.new_page()
                page.goto(backend.ApiTests.base + "/app")
                page.locator("[data-tab='settings']").click()
                page.locator("#f-lang").wait_for()
                for variant, language in (("A", "en"), ("B", "zh")):
                    with page.expect_response(lambda response: response.url.endswith("/api/config")
                                              and response.request.method == "POST") as saved:
                        page.locator("#f-lang").select_option(language)
                    if saved.value.status != 200 or api.request("/api/config")[1]["lang"] != language:
                        raise AssertionError("UI language was not persisted")
                    source = backend.ApiTests.vault / f"language-{variant}.html"
                    source.write_text(
                        f"<!doctype html><html><head><title>Aster latency fixture {variant}</title></head><body><article>"
                        "<p>This is a fictional benchmark report created solely for software testing, not a published result.</p>"
                        "<p>Under one CPU thread on 100 synthetic records, method Aster reduced elapsed time from 10 seconds "
                        "to 8 seconds compared with baseline B, at unchanged 90% accuracy.</p>"
                        "<p>Only this artificial dataset was measured. The authors make no claim about large-scale or real-world "
                        "generalization. No images were supplied.</p></article></body></html>", encoding="utf-8")
                    status, added = api.request("/api/queue", {"kind": "html", "value": str(source.resolve())})
                    if status != 200:
                        raise AssertionError(str(added))
                    item_id = added["item"]["id"]
                    status, started = api.request("/api/queue/run", {})
                    if status != 200 or not started.get("ok"):
                        raise AssertionError(str(started))
                    deadline = time.monotonic() + 150
                    while True:
                        items = api.request("/api/queue")[1]
                        item = next(row for row in items if row["id"] == item_id)
                        if item["status"] == "failed":
                            raise AssertionError(str(item.get("error")))
                        if item["status"] == "done":
                            break
                        if time.monotonic() >= deadline:
                            raise AssertionError("Queued generation timed out")
                        time.sleep(.25)
                    note = backend.ApiTests.vault / "papers" / item["note"]
                    text = note.read_text(encoding="utf-8")
                    headings = app.fulltext.SECTIONS_EN if language == "en" else app.fulltext.SECTIONS
                    for heading in headings:
                        if "## " + heading not in text:
                            raise AssertionError("Output heading has the wrong language: " + heading)
                    end = "## Source evidence" if language == "en" else "## 原文引用依据"
                    guide = text.split("## " + headings[0], 1)[1].split(end, 1)[0]
                    han = len(re.findall(r"[\u3400-\u9fff]", guide))
                    if (language == "en" and han != 0) or (language == "zh" and han < 20):
                        raise AssertionError("Actual model output did not follow the chosen language")
                    if original_note != existing.read_bytes():
                        raise AssertionError("Changing language rewrote an existing note")
                    report["checks"].append({"language": language, "note": str(note), "han_characters": han,
                                             "guide_preview": guide[:250]})
                    print(f"PASS: real queued model output follows settings language {language}", flush=True)
            finally:
                browser.close()
        report["result"] = "PASS"
    except Exception as exc:
        report["result"] = "FAIL"
        report["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        if fixture_started:
            if exe is not None and backend.ApiTests.proc.poll() is None:
                subprocess.run(["taskkill.exe", "/PID", str(backend.ApiTests.proc.pid), "/T", "/F"],
                               capture_output=True, check=False)
                backend.ApiTests.proc.wait(timeout=15)
            backend.ApiTests.tearDownClass()
        # start_server may have created a process even if health checking failed.
        model_process = model_process or app._server["proc"]
        if model_process is not None and model_process.poll() is None:
            subprocess.run(["taskkill.exe", "/PID", str(model_process.pid), "/T", "/F"], capture_output=True, check=False)
            model_process.wait(timeout=15)
        (run / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)


if __name__ == "__main__":
    main()
