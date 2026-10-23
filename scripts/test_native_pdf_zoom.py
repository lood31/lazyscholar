"""Opt-in packaged WebView2 PDF zoom acceptance using real Windows mouse input."""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import test_app_backend as backend
from playwright.sync_api import sync_playwright
from test_native_highlight import NativeMouse


def require(value, message):
    if not value:
        raise AssertionError(message)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def native_click(mouse, page, selector):
    box = page.locator(selector).bounding_box()
    if box is None:
        raise AssertionError("Native click target is not visible: " + selector)
    center = [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2]
    try:
        mouse.drag(page, center, center)
    except AssertionError:
        raise RuntimeError("Native click acceptance stopped; keep the isolated test window foreground and rerun.") from None
    page.wait_for_function("id => window._nativeZoomClicks?.some(row => row.id === id && row.trusted)", arg=selector[1:])


def metrics(page):
    return page.evaluate("""() => ({view:readerView,loaded:pdfLoaded,zoom:pdfZoom,
      percent:document.querySelector('#pdf-scale').textContent,
      width:document.querySelector('#pdf-wrap .pg canvas')?.getBoundingClientRect().width,
      clicks:window._nativeZoomClicks || []})""")


def audit_clicks(page):
    page.evaluate("""() => {window._nativeZoomClicks=[];document.addEventListener('click',event=>{
      const button=event.target.closest('button'); if(button) window._nativeZoomClicks.push({id:button.id,trusted:event.isTrusted});
    },true);}""")


def main():
    from html_fixture import seed_html

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--focus-wait", type=float, default=60, help="Seconds to allow the user to focus the isolated test window")
    parser.add_argument("--during-load", action="store_true", help="Also click zoom while later-page metadata is held")
    args = parser.parse_args()
    exe = args.exe.resolve()
    require(exe.is_file(), "Missing packaged EXE")
    run = backend.new_vault("native-pdf-zoom").resolve()
    (run / "papers" / "fixture.md").write_text("# Native PDF zoom fixture\n\narXiv: 2510.16720", encoding="utf-8")
    seed_html(run)
    http_port, debug_port = free_port(), free_port()
    env = dict(os.environ, LAZYSCHOLAR_UI_PORT=str(http_port), LAZYSCHOLAR_PREFERENCES=str(run / "preferences.json"),
               WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f"--remote-debugging-port={debug_port} --remote-allow-origins=*",
               WEBVIEW2_USER_DATA_FOLDER=str(run / "webview2"))
    base = f"http://127.0.0.1:{http_port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    report = {"exe": str(exe), "run": str(run), "checks": []}
    page = mouse = process = None
    log = (run / "native-app.log").open("w", encoding="utf-8")
    try:
        with sync_playwright() as pw:
            writer_browser = pw.chromium.launch(headless=True, executable_path=pw.chromium.executable_path)
            writer = writer_browser.new_page()
            writer.set_content("<style>section{break-after:page}section:last-child{break-after:auto}</style>" +
                               "".join(f"<section><h1>PDF page {n}</h1>" +
                                       "<p>Original PDF text for native zoom acceptance, without a model or generated note.</p>" * 8 +
                                       "</section>" for n in range(1, 5)))
            (run / "papers_pdf").mkdir(exist_ok=True)
            (run / "papers_pdf" / "2510.16720.pdf").write_bytes(writer.pdf(format="A4"))
            writer_browser.close()
            process = subprocess.Popen([str(exe), "--app", "--vault", str(run)], env=env, stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 60
            while True:
                try:
                    with opener.open(f"http://127.0.0.1:{debug_port}/json/version", timeout=1):
                        break
                except OSError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Native app did not expose its test WebView2; another app instance may already be running") from None
                    time.sleep(.15)
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}")
            context = browser.contexts[0]
            deadline = time.monotonic() + 30
            while page is None:
                page = next((candidate for candidate in context.pages if candidate.url == base + "/app"), None)
                if page is None:
                    require(time.monotonic() < deadline, "Main test WebView not found")
                    time.sleep(.1)
            page.set_default_timeout(10000)
            page.wait_for_function("!!window.pywebview?.api?.toggle_main")
            page.evaluate("window.pywebview.api.toggle_main()")
            page.locator("[data-tab='notes']").click()
            page.locator(".note").filter(has_text="Native PDF zoom fixture").click()
            page.locator("#paper-pages math").first.wait_for()
            page.locator("#btn-reader-pdf").click()
            page.wait_for_function("pdfLoaded")
            mouse = NativeMouse(args.focus_wait)
            audit_clicks(page)
            before = metrics(page)
            native_click(mouse, page, "#pdf-zoom-in")
            page.wait_for_function("pdfLoaded && pdfZoom === 1.75")
            after = metrics(page)
            require(after["width"] > before["width"] * 1.1, "Native plus click did not enlarge the rendered PDF")
            report["checks"].append({"native_sidebar_plus": {"before": before, "after": after}})
            page.screenshot(path=str(run / "native-sidebar-175.png"))
            native_click(mouse, page, "#pdf-zoom-out")
            page.wait_for_function("pdfLoaded && pdfZoom === 1.5")
            native_click(mouse, page, "#pdf-fit")
            page.wait_for_function("pdfLoaded && pdfZoom === null")
            report["checks"].append({"native_sidebar_minus_and_fit": metrics(page)})
            main_window = mouse.window
            with context.expect_page() as opened:
                native_click(mouse, page, "#btn-reader-expand")
            larger = opened.value
            larger.set_default_timeout(10000)
            larger.wait_for_function("pdfLoaded")
            audit_clicks(larger)
            mouse.window = mouse.user32.FindWindowW(None, "LazyScholar · 阅读")
            require(mouse.window, "Native larger reader window was not created")
            before = metrics(larger)
            native_click(mouse, larger, "#pdf-zoom-in")
            larger.wait_for_function("pdfLoaded && pdfZoom === 1.75")
            after = metrics(larger)
            require(after["width"] > before["width"] * 1.1, "Native larger-window plus did not enlarge the PDF")
            report["checks"].append({"native_larger_window_plus": {"before": before, "after": after}})
            larger.screenshot(path=str(run / "native-larger-175.png"))
            with larger.expect_event("close"):
                larger.evaluate("setTimeout(() => window.pywebview.api.close_reader(), 100)")
            mouse.window = main_window
            if args.during_load:
                page.locator("#btn-reader-close").click()
                page.evaluate("""() => {
                  const original=pdfjsLib;
                  const gate=new Promise(resolve=>{window._releasePdfMetadata=resolve;});
                  window.pdfjsLib={...original,getDocument:(...args)=>({promise:original.getDocument(...args).promise.then(document=>{
                    const getPage=document.getPage.bind(document);
                    document.getPage=async number=>{if(number===2){window._metadataHeld=true;await gate;}return getPage(number);};
                    return document;
                  })})};
                }""")
                page.locator(".note").filter(has_text="Native PDF zoom fixture").click()
                page.locator("#paper-pages math").first.wait_for()
                page.locator("#btn-reader-pdf").click()
                page.wait_for_function("window._metadataHeld === true")
                before = metrics(page)
                require(not before["loaded"] and before["width"], "Initial PDF was not paused after its first rendered page")
                native_click(mouse, page, "#pdf-zoom-in")
                try:
                    page.wait_for_function("pdfZoom === 1.75 && document.querySelector('#pdf-wrap .pg canvas')?.getBoundingClientRect().width > 1000", timeout=8000)
                except Exception:
                    report["during_load_failure"] = metrics(page)
                    raise
                finally:
                    page.evaluate("window._releasePdfMetadata()")
                page.wait_for_function("pdfLoaded && pdfZoom === 1.75")
                report["checks"].append({"native_zoom_during_metadata_load": {"before": before, "after": metrics(page)}})
            report["result"] = "PASS"
    except Exception as exc:
        report["result"] = "FAIL"
        report["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        if mouse is not None:
            mouse.restore()
        if process is not None and process.poll() is None:
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            process.wait(timeout=15)
        log.close()
        (run / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)


if __name__ == "__main__":
    main()
