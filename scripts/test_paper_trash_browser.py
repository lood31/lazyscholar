"""Whole-paper delete UI, epoch safety and Windows-style restore.

Default uses a fake OS provider in the test server. --native (or --exe PATH) is
opt-in and recycles/restores only this invocation's isolated fixture files.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import test_app_backend as backend
from accept_windows_recycle import restore_owned
from playwright.sync_api import sync_playwright

PAPER = "2510.16720"


def check(value, message):
    if not value:
        raise AssertionError(message)


def recover(vault: Path, record: dict, native: bool):
    completed = {str(vault / relative): location for relative, location in record["locations"].items()}
    if native:
        restore_owned(completed, vault)
    else:
        for original, location in completed.items():
            path, cached = Path(original), Path(location)
            check(not path.exists(), "Fake OS restore must not overwrite")
            path.parent.mkdir(parents=True, exist_ok=True)
            cached.rename(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--exe", type=Path)
    parser.add_argument("--native-window", action="store_true", help="Exercise the actual packaged WebView2 panel")
    args = parser.parse_args()
    if args.native_window and not args.exe:
        raise ValueError("--native-window requires --exe")
    debug_port = 0
    exe = args.exe.resolve() if args.exe else None
    use_native = args.native or exe is not None
    if exe is not None and not exe.is_file():
        raise ValueError("Missing packaged EXE")
    original_popen = subprocess.Popen
    def launch(command, **kwargs):
        vault = backend.ApiTests.vault
        nonlocal debug_port
        if exe is not None:
            command = [str(exe), "--app" if args.native_window else "--ui", "--vault", str(vault)]
            if args.native_window:
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1",0))
                    debug_port = sock.getsockname()[1]
                kwargs["env"].update(WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f"--remote-debugging-port={debug_port} --remote-allow-origins=*",
                                     WEBVIEW2_USER_DATA_FOLDER=str(vault/"webview2"))
                with patch.dict(os.environ,kwargs["env"]):
                    config = backend.app.load_config(vault)
                    config.update(auto_batch_enabled=False, llm_model="", llama_cpp="")
                    backend.app.save_config(vault,config)
        elif not use_native:
            bootstrap = (
                "import sys; from pathlib import Path;sys.path.insert(0,str(Path('scripts').resolve()));"
                "import test_app_backend as b; import windows_recycle;from test_paper_trash import FakeBin;"
                "v=Path(sys.argv[1]);provider=FakeBin(v.parent/'fake-windows-bin');"
                "windows_recycle.recycle=provider.recycle;b.app.serve_ui(v,open_browser=False)"
            )
            command = [sys.executable, "-B", "-c", bootstrap, str(vault)]
        return original_popen(command, **kwargs)
    with patch.object(backend.subprocess, "Popen", side_effect=launch):
        backend.ApiTests.setUpClass()
    records_to_recover = []
    try:
        with sync_playwright() as pw:
            if args.native_window:
                deadline = time.monotonic()+45
                while True:
                    try:
                        backend.ApiTests.opener.open(f"http://127.0.0.1:{debug_port}/json/version",timeout=1).close()
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Packaged WebView2 did not become ready") from None
                        time.sleep(.1)
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
            else:
                browser = pw.chromium.launch(headless=True, executable_path=pw.chromium.executable_path)
            try:
                context = browser.contexts[0] if args.native_window else browser.new_context(viewport={"width": 700, "height": 850})
                errors, native_dialogs = [], []
                context.on("page", lambda page: page.on("pageerror", lambda error: errors.append(str(error))))
                request = context.request
                base, vault = backend.ApiTests.base, backend.ApiTests.vault
                note_path = vault / "papers" / "fixture.md"
                note_path.write_text(note_path.read_text(encoding="utf-8") + "\n\n- Paper ID: " + PAPER + "v2\n", encoding="utf-8")
                original_note = note_path.read_bytes()
                pdf = vault / "papers_pdf" / (PAPER + ".pdf")
                original_pdf = pdf.read_bytes()
                external = vault / (PAPER + "v2.pdf")
                external.write_bytes(original_pdf)
                added = request.post(base + "/api/queue", data={"kind":"pdf","value":str(external)})
                check(added.ok, "Cannot seed queue")
                row = added.json()["item"]
                marks_url = base + "/api/highlights?id=" + PAPER
                revision = request.get(marks_url).json()["revision"]
                mark = {"page":1,"x":.1,"y":.2,"w":.3,"h":.04}
                check(request.post(marks_url,data={"revision":revision,"highlights":[mark]}).ok, "Cannot seed annotation")
                if args.native_window:
                    page = next(candidate for candidate in context.pages if candidate.url == base+"/app")
                    page.wait_for_function("!!window.pywebview?.api?.toggle_main")
                    page.evaluate("window.pywebview.api.toggle_main()")
                    page.on("pageerror",lambda error:errors.append(str(error)))
                else:
                    page = context.new_page()
                def reject_native(dialog):
                    native_dialogs.append(dialog.type)
                    dialog.dismiss()
                page.on("dialog", reject_native)
                if not args.native_window:
                    page.goto(base + "/app")
                else:
                    page.evaluate("refreshNotes()")
                page.locator("[data-tab='notes']").click()
                page.locator(".note").filter(has_text="A real note").click()
                page.locator("#paper-pages math").first.wait_for()
                with context.expect_page() as opened:
                    page.locator("#btn-reader-expand").click()
                reader = opened.value
                reader.locator("#paper-pages math").first.wait_for()
                check(reader.evaluate("curArxiv") == PAPER, "Version metadata was not canonicalized")
                old_epoch = reader.evaluate("curLifecycle")
                page.locator("#btn-reader-close").click()
                if args.native_window:
                    page.wait_for_function("innerWidth <= 440")
                check(page.locator("[data-trash-note],#btn-paper-trash").count() == 0, "Private trash/card action remains")
                page.locator("#delete-notes").click()
                page.locator(".note").filter(has_text="A real note").click()
                page.locator("#delete-notes-selected").click()
                page.locator("#paper-action-dialog").wait_for(state="visible")
                page.screenshot(path=str(backend.RUN/"paper-delete-confirm.png"))
                page.locator("#paper-action-cancel").click()
                check(pdf.exists(), "Cancel moved a file")
                page.locator("#delete-notes-selected").click()
                page.locator("#paper-action-dialog").wait_for(state="visible")
                with page.expect_response(lambda response: response.url.endswith("/api/papers/delete")) as deleted:
                    page.locator("#paper-action-confirm").click()
                record = backend.app.paper_trash._native(vault).get(PAPER)
                if record and record.get("locations"):
                    records_to_recover.append(record)
                check(deleted.value.ok, "Delete failed: " + deleted.value.text())
                if record is None:
                    raise AssertionError("Deletion receipt was not saved")
                if not reader.is_closed():
                    reader.wait_for_event("close", timeout=5000)
                page.locator(".note").filter(has_text="A real note").wait_for(state="detached")
                check(not pdf.exists() and not note_path.exists(), "Paper files remain in original locations")
                check(not (vault / ".paper-trash").exists(), "New private payload storage was created")
                check(external.read_bytes() == original_pdf, "External original changed")
                for suffix in (PAPER,PAPER+"v2"):
                    for endpoint in ("/api/pdf?id=","/api/paper?id=","/api/reading-highlights?id=","/api/highlights?id="):
                        check(request.get(base+endpoint+suffix).status == 410, "Deleted ID remains readable")
                check(request.post(marks_url,data={"revision":revision,"highlights":[]},headers={"X-Paper-Lifecycle":old_epoch}).status == 410,
                      "Stale write recreated deleted annotations")
                recover(vault,record,use_native)
                records_to_recover.remove(record)
                page.evaluate("refreshNotes()")
                page.locator(".note").filter(has_text="A real note").wait_for()
                check(pdf.read_bytes() == original_pdf and note_path.read_bytes() == original_note, "System restore changed content")
                metadata = request.get(base+"/api/note?kind=papers&name=fixture.md").json()
                fresh_epoch = metadata["lifecycle"]
                check(fresh_epoch != old_epoch, "Restore did not invalidate old reader sessions")
                check(request.get(marks_url,headers={"X-Paper-Lifecycle":fresh_epoch}).json()["highlights"] == [mark], "Annotations not restored")
                check(request.post(marks_url,data={"revision":revision,"highlights":[]},headers={"X-Paper-Lifecycle":old_epoch}).status == 410,
                      "Old reader upgraded through a restore")
                check(request.get(base+"/api/pdf?id="+PAPER+"v2").body() == original_pdf, "Versioned alias created different data")
                # The queue uses the same top-level single-selection flow.
                page.locator("[data-tab='queue']").click()
                page.locator("#delete-queue").click()
                page.locator(f'[data-queue-card="{row["id"]}"]').click()
                page.locator("#delete-queue-selected").click()
                page.locator("#paper-action-dialog").wait_for(state="visible")
                with page.expect_response(lambda response: response.url.endswith("/api/papers/delete")) as deleted_again:
                    page.locator("#paper-action-confirm").click()
                record = backend.app.paper_trash._native(vault).get(PAPER)
                if record and record.get("locations"):
                    records_to_recover.append(record)
                check(deleted_again.value.ok, "Queue deletion failed")
                if record is None:
                    raise AssertionError("Deletion receipt was not saved")
                import_reply = request.post(base+"/api/queue",data={"kind":"pdf","value":str(external)})
                check(import_reply.status == 410 and import_reply.json()["reimport_id"], "Reimport bypassed explicit consent")
                info = import_reply.json()
                reimported = request.post(base+"/api/queue",data={"kind":"pdf","value":str(external),"paper_id":PAPER,"reactivate":info["reimport_id"]})
                check(reimported.ok, "Explicit reimport failed: " + reimported.text())
                check(request.post(marks_url,data={"revision":revision,"highlights":[]},headers={"X-Paper-Lifecycle":fresh_epoch}).status == 410,
                      "An old operation wrote through a reimport")
                check(external.read_bytes() == original_pdf, "Reimport touched external source")
                check(not native_dialogs and not errors, "UI errors or native confirmation: " + str(errors))
                # Retain any new test copies rather than overwriting them during cleanup.
                for relative in record["locations"]:
                    target = vault / relative
                    if target.exists():
                        saved = vault.parent / "new-test-copies" / relative
                        saved.parent.mkdir(parents=True,exist_ok=True)
                        target.rename(saved)
                recover(vault,record,use_native)
                records_to_recover.remove(record)
                report = {"result":"PASS","native":use_native,"native_window":args.native_window,"exe":str(exe) if exe else None,
                          "checks":["top-level selection","compact confirmation","cancel","paper and queue delete","external originals",
                                    "system restore","versioned IDs","old-epoch rejection","explicit reimport","no private payload"]}
                (backend.RUN/"paper-delete-browser.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
                print("PASS: " + ("real Windows" if use_native else "fake-provider") + " delete/restore UI, queue and stale-epoch safety; artifacts: " + str(backend.RUN))
            finally:
                browser.close()
    finally:
        for record in records_to_recover:
            # Only this invocation's fixture paths are eligible for test recovery.
            for relative in record["locations"]:
                target = backend.ApiTests.vault / relative
                if target.exists():
                    saved = backend.ApiTests.vault.parent / ("retained-"+record["id"]) / relative
                    saved.parent.mkdir(parents=True,exist_ok=True)
                    target.rename(saved)
            recover(backend.ApiTests.vault,record,use_native)
        if exe is not None and backend.ApiTests.proc.poll() is None:
            subprocess.run(["taskkill.exe","/PID",str(backend.ApiTests.proc.pid),"/T","/F"],capture_output=True,check=False)
            backend.ApiTests.proc.wait(timeout=15)
        backend.ApiTests.tearDownClass()


if __name__ == "__main__":
    main()
