"""Real Windows app + local Gemma acceptance. No mocked model or arXiv data.

Run after building: python -B scripts/accept_app.py --exe dist/LazyScholar.exe
Artifacts are retained under qa/acceptance; the user's vault is never opened.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import lazyscholar as app  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def require(value, message):
    if not value:
        raise AssertionError(message)


def wait_for(probe, timeout=60):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            result = probe()
            if result:
                return result
        except (OSError, ValueError) as e:
            last = e
        threading.Event().wait(0.2)
    raise TimeoutError(f"Acceptance wait expired: {last}")


def choose_native_folder(path: Path) -> None:
    """Drive only the native folder dialog owned by this isolated app."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetDlgCtrlID.argtypes = (wintypes.HWND,)
    user32.GetParent.argtypes = (wintypes.HWND,)
    user32.GetParent.restype = wintypes.HWND
    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.SendMessageW.restype = wintypes.LPARAM
    user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.IsWindow.argtypes = (wintypes.HWND,)
    user32.EnumWindows.argtypes = (callback, wintypes.LPARAM)
    user32.EnumChildWindows.argtypes = (wintypes.HWND, callback, wintypes.LPARAM)
    main_window = user32.FindWindowW(None, "LazyScholar · 懒人学者")
    require(main_window, "Isolated app window not found")
    owner_pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(main_window, ctypes.byref(owner_pid))
    seen = []

    def label(handle, class_name=False):
        buffer = ctypes.create_unicode_buffer(512)
        if class_name:
            user32.GetClassNameW(handle, buffer, len(buffer))
        else:
            user32.GetWindowTextW(handle, buffer, len(buffer))
        return buffer.value

    def locate():
        nonlocal seen
        dialogs = []
        @callback
        def collect(handle, _):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            if pid.value == owner_pid.value and label(handle,True) == "#32770":
                dialogs.append(handle)
            return True
        user32.EnumWindows(collect,0)
        seen = []
        for dialog in dialogs:
            controls = []
            @callback
            def child(handle, _, found=controls):
                found.append((handle,label(handle,True),user32.GetDlgCtrlID(handle),label(handle)))
                return True
            user32.EnumChildWindows(dialog,child,0)
            fields = [h for h,kind,ident,_ in controls if kind == "Edit" and
                      (ident in (1148, 1152) or user32.GetDlgCtrlID(user32.GetParent(h)) == 1148)]
            buttons = [h for h,kind,ident,_ in controls if kind == "Button" and ident == 1]
            seen.extend((kind,ident,text) for _,kind,ident,text in controls if kind in ("Edit","Button","ComboBox","ComboBoxEx32"))
            if fields and buttons:
                return dialog,fields[0],buttons[0]
        return None

    try:
        dialog, field, button = wait_for(locate,timeout=15)
    except TimeoutError as e:
        raise RuntimeError(f"Native dialog controls not located: {seen}") from e
    value = ctypes.create_unicode_buffer(str(path))
    require(user32.SendMessageW(field,0x000C,0,ctypes.cast(value,ctypes.c_void_p).value), "Native folder field rejected the path")
    # Commit the typed folder just as Enter in the native field does. Merely
    # setting its text can leave IFileDialog's selected folder unchanged.
    user32.SendMessageW(dialog,0x0028,field,1)
    user32.PostMessageW(field,0x0100,0x0D,0x001C0001)
    user32.PostMessageW(field,0x0101,0x0D,0xC01C0001)
    threading.Event().wait(0.5)
    if user32.IsWindow(button):
        user32.SendMessageW(button,0x00F5,0,0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--resume-run", type=Path, help="Resume only folder-picker/restart checks after a recorded model acceptance")
    args = parser.parse_args()
    exe = args.exe.resolve()
    require(exe.is_file(), f"Missing executable: {exe}")
    run = (args.resume_run.resolve() if args.resume_run else
           ROOT / "qa" / "acceptance" / datetime.now().strftime("%Y%m%d-%H%M%S"))
    vault = run / "original-vault"
    selected = run / "selected-vault"
    app._ensure_vault(vault)
    selected.mkdir(parents=True, exist_ok=True)
    preferences = run / "preferences.json"
    app._write_json(preferences, {"vault": str(vault)})
    port, debug_port, model_port = free_port(), free_port(), free_port()
    cfg = app.load_config(vault)
    require(Path(cfg["model_path"]).is_file(), "A real local GGUF model is required")
    cfg.update(port=model_port, schedule="03:17")
    app.save_config(vault, cfg)
    env = dict(os.environ, LAZYSCHOLAR_PREFERENCES=str(preferences), LAZYSCHOLAR_UI_PORT=str(port),
               WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f"--remote-debugging-port={debug_port}",
               WEBVIEW2_USER_DATA_FOLDER=str(run / "webview-profile"), PYTHONIOENCODING="utf-8")
    env.pop("LAZYSCHOLAR_VAULT", None)
    base = f"http://127.0.0.1:{port}"
    results: dict[str, Any] = {"exe": str(exe), "vault": str(vault), "model": cfg["model_path"], "checks": []}
    if args.resume_run:
        try:
            results = json.loads((run / "result.json").read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            raise ValueError("Cannot read the saved acceptance result") from e
        require(any(c["name"] == "real_note_generated_and_model_stopped" for c in results["checks"]), "Resume requires recorded real-model acceptance")
        results.pop("error", None)
    process = None
    browser = None
    page = None
    log = (run / "process.log").open("w", encoding="utf-8")

    def record(name, detail: Any = True):
        results["checks"].append({"name": name, "detail": detail})
        app._write_json(run / "result.json", results)
        print(name, detail, flush=True)

    with sync_playwright() as pw:
        client = pw.request.new_context(base_url=base, timeout=5000)

        def get(path):
            response = client.get(path)
            require(response.ok, f"HTTP {response.status}: {path}")
            return response.json()

        def launch(expected_vault):
            nonlocal process, browser, page
            process = subprocess.Popen([str(exe), "--app", "--interval", "300"], env=env, stdout=log, stderr=subprocess.STDOUT)
            def ready():
                require(process is not None and process.poll() is None, "Packaged app exited before becoming ready")
                try:
                    return get("/api/storage")
                except Exception:
                    return None
            state = wait_for(ready)
            require(Path(state["active"]) == expected_vault, "Restart opened the wrong vault")
            def browser_ready():
                try:
                    return client.get(f"http://127.0.0.1:{debug_port}/json/version").ok
                except Exception:
                    return False
            wait_for(browser_ready, timeout=60)
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}", timeout=30000)
            page = browser.contexts[0].pages[0]
            page.set_default_timeout(20000)
            page.wait_for_function("window.pywebview && window.pywebview.api")
            page.evaluate("window.pywebview.api.toggle_main()")
            page.wait_for_function("document.querySelector('#f-model').value.length > 0")
            return page

        def quit_app():
            nonlocal process, browser, page
            if page is None or process is None:
                raise RuntimeError("App is not running")
            page.evaluate("window.pywebview.api.quit_app()")
            process.wait(timeout=30)
            process = None
            browser = None
            page = None

        try:
            if not args.resume_run:
                page = launch(vault)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                heights = {}
                for name in ("queue", "notes", "model", "settings"):
                    page.locator(f"[data-tab='{name}']").click()
                    heights[name] = page.evaluate("window.innerHeight")
                require(len(set(heights.values())) == 1, "Tabs changed panel height")
                record("native_tab_heights", heights)
                page.screenshot(path=str(run / "settings.png"))
                page.locator("[data-tab='queue']").click()
                require("03:17" in page.locator("#page-queue .sub").inner_text(), "Schedule label is stale")
                page.locator("#q-input").fill("https://arxiv.org/abs/2510.16720")
                page.locator("#btn-add").click()
                page.locator(".st-pending").wait_for()
                page.locator("#btn-run").click()
                page.locator(".st-processing").wait_for(timeout=150000)
                state = get("/api/state")
                require(state["llama_up"] and state["managed"] and state["batch_running"], "Real local model was not running during the batch")
                record("real_local_model_running", state)
                page.wait_for_function("document.querySelector('.st-done, .st-failed')", timeout=360000)
                rows = get("/api/queue")
                require(rows[0]["status"] == "done", f"Real inference failed: {rows}")
                wait_for(lambda: not get("/api/state")["batch_running"], timeout=30)
                require(not get("/api/state")["llama_up"], "Batch-owned model was not stopped")
                note_path = vault / "papers" / rows[0]["note"]
                note = note_path.read_text(encoding="utf-8")
                for heading in ("一句话总结", "中文导读", "核心要点", "关键词汇", "跟读原句"):
                    require(heading in note, f"Generated note missing section: {heading}")
                record("real_note_generated_and_model_stopped", {"note": str(note_path), "characters": len(note)})
                page.screenshot(path=str(run / "queue-done.png"))
                page.locator("[data-tab='notes']").click()
                page.locator(".note").first.click()
                page.locator("#paper-wrap .paper-text").first.wait_for()
                page.wait_for_function("!document.querySelector('#btn-hl').disabled")
                require(page.locator("#pdf-wrap canvas").count() == 0, "Default view is still a PDF canvas")
                before = page.locator("#paper-wrap").evaluate("el => parseFloat(getComputedStyle(el).fontSize)")
                page.locator("#reader-font-up").click()
                after = page.locator("#paper-wrap").evaluate("el => parseFloat(getComputedStyle(el).fontSize)")
                require(after > before, "Reflow font size did not increase")
                page.locator("#btn-hl").click()
                page.evaluate("""() => {
                  const el = [...document.querySelectorAll('#paper-pages [data-block]')].find(e => e.textContent.length > 30);
                  const range = document.createRange(); range.setStart(el.firstChild,0); range.setEnd(el.firstChild,25);
                  const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
                  document.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
                }""")
                page.locator("#paper-wrap mark").first.wait_for()
                highlights = get("/api/reading-highlights?id=2510.16720")["highlights"]
                require(len(highlights) > 0, "Reflow text highlight did not persist")
                record("native_reflow_text_font_and_highlight", {"font_before":before,"font_after":after,"highlights":len(highlights)})
                page.screenshot(path=str(run / "native-article-title.png"))
                for _ in range(6):
                    if page.locator("#paper-wrap figure img").count():
                        break
                    page.evaluate("loadNextPaper()")
                page.locator("#paper-wrap figure img").first.wait_for()
                page.locator("#paper-wrap figure img").first.click()
                require(page.locator("#image-zoom").is_visible(), "Original figure crop did not zoom")
                page.screenshot(path=str(run / "native-figure-zoom.png"))
                page.locator("#zoom-close").click()
                page.screenshot(path=str(run / "native-article.png"))
                record("native_figures_extracted_and_zoomable")
                page.locator("#btn-reader-note").click()
                page.locator("#note-md h2").first.wait_for()
                require("Source scope" in page.locator("#note-md").inner_text(), "Abstract-only provenance is missing")
                page.screenshot(path=str(run / "note.png"))
                require(not errors, f"JavaScript errors: {errors}")
                quit_app()

            page = launch(vault)
            if not args.resume_run:
                page.locator("[data-tab='notes']").click()
                page.locator(".note").first.click()
                page.locator("#paper-wrap mark").first.wait_for(timeout=90000)
                page.wait_for_function("!document.querySelector('#btn-hl-clear').disabled", timeout=90000)
                record("reflow_highlight_survived_packaged_app_restart")
                page.locator("#btn-hl-clear").click()
                wait_for(lambda: get("/api/reading-highlights?id=2510.16720")["highlights"] == [], timeout=10)
                record("reflow_highlight_clear_persisted")
                page.locator("#btn-reader-close").click()
            page.locator("[data-tab='settings']").click()
            retained_note = Path(next(c["detail"]["note"] for c in results["checks"]
                                      if c["name"] == "real_note_generated_and_model_stopped"))
            page.locator("#btn-vault").click()
            choose_native_folder(selected)
            page.wait_for_function("path => document.querySelector('#f-vault').value === path", arg=str(selected))
            state = get("/api/storage")
            require(state["restart_required"] and Path(state["active"]) == vault, "Folder switch took effect before restart")
            require(retained_note.exists() and not list(selected.iterdir()), "Changing folders moved old files")
            record("native_folder_picker_saved_without_moving_files", state)
            page.screenshot(path=str(run / "settings-selected.png"))
            quit_app()

            page = launch(selected)
            require(get("/api/queue") == [], "New vault did not start with its own queue")
            require(retained_note.exists(), "Original note disappeared on restart")
            record("selected_folder_used_after_restart")
            page.screenshot(path=str(run / "selected-vault.png"))
            quit_app()
            results["passed"] = True
            app._write_json(run / "result.json", results)
            print(f"ACCEPTED: {run}", flush=True)
            return 0
        except Exception as error:
            results["passed"] = False
            results["error"] = str(error)
            if page is not None:
                try:
                    results["ui_message"] = page.locator("#app-message").inner_text(timeout=1000)
                    page.screenshot(path=str(run / "failure.png"))
                except Exception as capture_error:
                    results["capture_error"] = str(capture_error)
            app._write_json(run / "result.json", results)
            print(f"ACCEPTANCE FAILED: {error}\nArtifacts: {run}", flush=True)
            raise
        finally:
            if process is not None and process.poll() is None:
                try:
                    client.post("/api/model/stop", data="{}", headers={"Content-Type": "application/json"})
                    if page is not None:
                        page.evaluate("window.pywebview.api.quit_app()")
                    process.wait(timeout=10)
                except Exception:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            client.dispose()
            log.close()


if __name__ == "__main__":
    raise SystemExit(main())
