"""Regression for real Windows mouse selection in the packaged WebView.

No DOM-created selections, model, network PDF, or user's vault. Optional
--live-html fetches arXiv HTML into an empty isolated cache. Briefly moves
and restores the mouse; all clicks are restricted to the isolated app window.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path

from accept_app import ROOT, app, free_port, require, wait_for
from html_fixture import seed_html
from pdf_fixture import paper_fixture
from playwright.sync_api import sync_playwright


class NativeMouse:
    def __init__(self, focus_wait=1.0):
        self.focus_wait = focus_wait
        class MouseInput(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]
        class InputUnion(ctypes.Union):
            _fields_ = [("mi", MouseInput)]
        class Input(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", InputUnion)]
        self.Input = Input
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
        self.user32.FindWindowW.restype = wintypes.HWND
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        self.user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        self.user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        self.user32.GetAsyncKeyState.restype = ctypes.c_short
        self.user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        self.user32.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        self.user32.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
        self.user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
        self.user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int)
        self.window = self.user32.FindWindowW(None, "LazyScholar · 懒人学者")
        require(self.window, "Isolated native window not found")
        self.original = wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(self.original))

    def bounds(self):
        rect = wintypes.RECT()
        require(self.user32.GetWindowRect(self.window, ctypes.byref(rect)), "Cannot read native window bounds")
        return [rect.left, rect.top, rect.right, rect.bottom]

    def button(self, flags):
        event = self.Input()
        event.type = 0
        event.u.mi.dwFlags = flags
        require(self.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) == 1, "Windows rejected mouse input")

    def focus(self):
        self.user32.SetForegroundWindow(self.window)
        deadline = time.monotonic() + self.focus_wait
        while self.user32.GetForegroundWindow() != self.window and time.monotonic() < deadline:
            threading.Event().wait(0.05)
        foreground = self.user32.GetForegroundWindow()
        title = ctypes.create_unicode_buffer(256)
        self.user32.GetWindowTextW(foreground, title, len(title))
        require(foreground == self.window, f"Not sending mouse input: foreground is {title.value!r}, not the test app")
        def buttons_up():
            return all(not self.user32.GetAsyncKeyState(key) & 0x8000 for key in (0x01, 0x02, 0x04))
        while not buttons_up() and time.monotonic() < deadline:
            threading.Event().wait(0.02)
        require(buttons_up(), "Not sending mouse input while the owner's mouse button is held")
        threading.Event().wait(0.2)
        require(self.user32.GetForegroundWindow() == self.window and buttons_up(), "Input handoff did not settle safely")

    def drag(self, page, start, end):
        self.focus()
        rect, origin = wintypes.RECT(), wintypes.POINT(0, 0)
        self.user32.GetClientRect(self.window, ctypes.byref(rect))
        self.user32.ClientToScreen(self.window, ctypes.byref(origin))
        width = page.evaluate("window.innerWidth")
        scale = (rect.right - rect.left) / width
        require(all(0 <= point[0] < width and 0 <= point[1] < (rect.bottom - rect.top) / scale for point in (start, end)),
                "Not sending mouse input outside the isolated test window")
        sx, sy = round(origin.x + start[0] * scale), round(origin.y + start[1] * scale)
        ex, ey = round(origin.x + end[0] * scale), round(origin.y + end[1] * scale)
        require(self.user32.SetCursorPos(sx, sy), "Cannot position test cursor")
        self.button(0x0002)
        try:
            for index in range(1, 16):
                require(self.user32.GetForegroundWindow() == self.window, "Test app lost focus during mouse drag")
                self.user32.SetCursorPos(round(sx + (ex - sx) * index / 15), round(sy + (ey - sy) * index / 15))
                threading.Event().wait(0.012)
        finally:
            self.button(0x0004)
        page.wait_for_timeout(120)

    def select_text(self, page, selector, start=0, end=16):
        element = page.locator(selector).first
        element.evaluate("el => el.scrollIntoView({block:'center',inline:'nearest'})")
        points = element.evaluate("""(el, offsets) => {
          const walker = document.createTreeWalker(el,NodeFilter.SHOW_TEXT);
          let node;
          while ((node=walker.nextNode()) && node.textContent.length < 12) {}
          if (!node) throw Error('No suitable text node for physical mouse drag');
          const first=document.createRange(), last=document.createRange();
          const start=offsets.start,end=Math.min(offsets.end,node.textContent.length);
          if(start<0||start>=end) throw Error('Invalid physical selection offsets');
          first.setStart(node,start); first.setEnd(node,start+1);
          last.setStart(node,end-1); last.setEnd(node,end);
          const a=first.getBoundingClientRect(), b=last.getBoundingClientRect();
          const hit=document.elementFromPoint(a.left+0.5,(a.top+a.bottom)/2);
          return {start:[a.left+0.5,(a.top+a.bottom)/2],end:[b.right-0.5,(b.top+b.bottom)/2],hit:hit ? hit.outerHTML.slice(0,300):null};
        }""", {"start": start, "end": end})
        self.drag(page, points["start"], points["end"])
        return points

    def restore(self):
        self.user32.SetCursorPos(self.original.x, self.original.y)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--arxiv", default="2510.16720")
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--live-html", action="store_true")
    parser.add_argument("--focus-wait", type=float, default=1.0, help="Seconds to allow manually focusing the isolated test window (0–120)")
    args = parser.parse_args()
    if not 0 <= args.focus_wait <= 120:
        parser.error("--focus-wait must be between 0 and 120 seconds")
    arxiv_id = args.arxiv
    if args.live_html and not args.pdf:
        parser.error("--live-html requires a local --pdf for the comparison check")
    run = ROOT / "qa" / "mouse-tests" / uuid.uuid4().hex[:10]
    vault = run / "vault"
    app._ensure_vault(vault)
    (vault / "papers_pdf").mkdir()
    (vault / "papers_pdf" / f"{arxiv_id}.pdf").write_bytes(args.pdf.read_bytes() if args.pdf else paper_fixture())
    (vault / "papers" / "fixture.md").write_text(f"# Mouse selection fixture\n\n## Note\nLocal fixture\n\narXiv: {arxiv_id}", encoding="utf-8")
    if not args.live_html:
        seed_html(vault, arxiv_id)
    require(not args.live_html or not (vault / "html_reader").exists(), "Live acquisition cache was not empty")
    app.save_config(vault, {"schedule": "03:17"})
    preferences = run / "preferences.json"
    app._write_json(preferences, {"vault": str(vault)})
    port, debug = free_port(), free_port()
    env = dict(os.environ, LAZYSCHOLAR_PREFERENCES=str(preferences), LAZYSCHOLAR_UI_PORT=str(port),
               WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f"--remote-debugging-port={debug}",
               WEBVIEW2_USER_DATA_FOLDER=str(run / "webview-profile"))
    env.pop("LAZYSCHOLAR_VAULT", None)
    result = {"exe": str(args.exe.resolve()), "exe_sha256": hashlib.sha256(args.exe.read_bytes()).hexdigest(),
              "arxiv": arxiv_id, "live_html": args.live_html, "passed": False, "checks": []}
    log = (run / "process.log").open("w", encoding="utf-8")
    process = subprocess.Popen([str(args.exe.resolve()), "--app"], env=env, stdout=log, stderr=subprocess.STDOUT)
    page, mouse = None, None
    try:
        with sync_playwright() as pw:
            client = pw.request.new_context(timeout=3000)
            def ready(url):
                require(process.poll() is None, "Test app exited; close the existing LazyScholar instance before testing")
                try:
                    return client.get(url).ok
                except Exception:
                    return False
            wait_for(lambda: ready(f"http://127.0.0.1:{debug}/json/version"))
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{debug}")
            page = browser.contexts[0].pages[0]
            page.wait_for_function("window.pywebview && window.pywebview.api && settingsLoaded")
            page.evaluate("window.pywebview.api.toggle_main()")
            page.locator("[data-tab='notes']").click()
            page.locator(".note").first.click()
            page.locator("#paper-wrap math").first.wait_for(timeout=60000)
            page.wait_for_function("readerView === 'paper' && paperState && paperState.source && !document.querySelector('#btn-hl').disabled")
            index = vault / "html_reader" / arxiv_id / "article.json"
            document = json.loads(index.read_text(encoding="utf-8"))
            result["html_source"] = document["source"]
            result["math_count"] = document["math_count"]
            result["source_url"] = document["source_url"]
            result["html_sha256"] = hashlib.sha256((index.parent / "article.html").read_bytes()).hexdigest()
            if args.live_html:
                require(arxiv_id != "2610.30130" or document["math_count"] == 2039, "Problem paper has unexpected math-node count")
                result["checks"].append("automatic_empty_cache_html_acquisition_default_transport")
            part = document["anchors"]["ls-source-S2.p1.m2"]
            page.evaluate("async n => { while(paperState.next<=n) { if(!await loadNextPaper()) throw Error('Part load failed'); } }", part)
            math = page.locator("[id='ls-source-S2.p1.m2']")
            math.scroll_into_view_if_needed()
            geometry = math.evaluate("""el => {
              const s=el.querySelector('msubsup'), r=n=>{const b=n.getBoundingClientRect();return {top:b.top,bottom:b.bottom,width:b.width,height:b.height};};
              return {namespace:el.namespaceURI,display:getComputedStyle(el).display,base:r(s.children[0]),lower:r(s.children[1]),upper:r(s.children[2])};
            }""")
            require(geometry["namespace"] == "http://www.w3.org/1998/Math/MathML", "Math lost its namespace")
            require(geometry["upper"]["top"] < geometry["base"]["top"] and geometry["lower"]["bottom"] > geometry["base"]["bottom"], "Superscript/subscript layout is flattened")
            result["math_geometry"] = geometry
            page.screenshot(path=str(run / "native-mathml.png"))
            page.locator("#paper-wrap mfrac").first.wait_for()
            fraction = page.locator("#paper-wrap mfrac").first.evaluate("""el => {
              const a=el.children[0].getBoundingClientRect(),b=el.children[1].getBoundingClientRect();
              return {numerator_bottom:a.bottom,denominator_top:b.top};
            }""")
            require(fraction["numerator_bottom"] <= fraction["denominator_top"] + 1, "Fraction numerator/denominator overlap")
            result["fraction_geometry"] = fraction
            page.locator("#paper-wrap math").first.click()
            page.locator("#math-zoom[open]").wait_for()
            page.screenshot(path=str(run / "native-formula-zoom.png"))
            page.locator("#math-zoom-close").click()
            result["checks"].append("native_mathml_scripts_fractions_and_zoom")
            result["computed_user_select"] = page.locator("#paper-wrap .ls-text").first.evaluate("el => getComputedStyle(el).userSelect")
            result["page_errors"] = []
            page.on("pageerror", lambda error: result["page_errors"].append(str(error)))
            page.evaluate("""() => {
              window._mouseAudit=[];
              for (const name of ['mousedown','mouseup','click']) document.addEventListener(name,e => {
                window._mouseAudit.push({type:name,mode:hlMode,view:readerView,selected:window.getSelection().toString(),
                  id:e.target.id,tag:e.target.tagName,x:e.clientX,y:e.clientY,detail:e.detail});
              },true);
            }""")
            mouse = NativeMouse(args.focus_wait)
            if args.focus_wait > 1:
                page.evaluate("showMessage('请点一下测试窗口的标题栏（不要拖动），随后暂时停用鼠标键盘。',false)")
                print("Waiting for the test window to become foreground; click its titlebar without dragging.", flush=True)
            mouse.focus()
            before = mouse.bounds()
            result["copy_points"] = mouse.select_text(page,"#paper-wrap .ls-text")
            result["copy_events"] = page.evaluate("window._mouseAudit.slice(-20)")
            selected = page.evaluate("window.getSelection().toString()")
            result["selected_text"] = selected
            require(len(selected) >= 5, "Physical mouse could not select article text")
            require(mouse.bounds() == before, "Selecting article text dragged the native window")
            result["checks"].append("physical_article_selection_without_window_drag")
            # End the separate copy-selection check before testing highlight
            # mode: dragging an already selected range starts native text DnD.
            page.locator("#r-title").click()
            require(page.evaluate("window.getSelection().isCollapsed"), "Copy-selection check left an active range")
            page.locator("#btn-hl").click()
            require(page.evaluate("hlMode"), "Highlight mode was not enabled")
            mouse.select_text(page,"#paper-wrap .ls-text")
            result["after_drag"] = page.evaluate("({mode:hlMode,view:readerView,selected:window.getSelection().toString(),events:window._mouseAudit,error:document.querySelector('#app-message').textContent})")
            page.locator("#paper-wrap mark").first.wait_for(timeout=5000)
            marks = client.get(f"http://127.0.0.1:{port}/api/reading-highlights?id={arxiv_id}").json()
            require(marks["highlights"], "Physical selection did not persist a highlight")
            result["checks"].append("physical_article_highlight_saved")
            page.screenshot(path=str(run / "mouse-highlight.png"))
            page.locator("#btn-reader-close").click()
            page.locator(".note").first.click()
            page.locator("#paper-wrap mark").first.wait_for()
            result["checks"].append("physical_highlight_reopened")
            original = marks["highlights"][0]
            page.evaluate("""() => {
              [...document.querySelectorAll('#paper-wrap p .ls-text')].find(el=>el.textContent.length>30).dataset.nativeEraseOther='yes';
            }""")
            page.locator("#btn-hl").click()
            mouse.select_text(page,'#paper-wrap [data-native-erase-other="yes"]')
            page.evaluate("async () => { await highlightWrite; }")
            before_erase = client.get(f"http://127.0.0.1:{port}/api/reading-highlights?id={arxiv_id}").json()["highlights"]
            others = [row for row in before_erase if row["block"] != original["block"]]
            require(others, "Unrelated physical highlight was not created")
            page.locator("#btn-hl-clear").click()
            require(page.evaluate("eraseMode && !hlMode"), "Eraser mode did not replace highlight mode")
            require(client.get(f"http://127.0.0.1:{port}/api/reading-highlights?id={arxiv_id}").json()["highlights"] == before_erase,
                    "Eraser button cleared highlights before a selection")
            mouse.select_text(page, f'#paper-wrap [data-block="{original["block"]}"] mark', start=3, end=9)
            page.evaluate("async () => { await highlightWrite; }")
            erased = client.get(f"http://127.0.0.1:{port}/api/reading-highlights?id={arxiv_id}").json()["highlights"]
            fragments = sorted((row["start"], row["end"]) for row in erased if row["block"] == original["block"])
            require(fragments == [(original["start"], original["start"] + 3), (original["start"] + 9, original["end"])],
                    "Physical erase did not retain both unselected ends")
            require([row for row in erased if row["block"] != original["block"]] == others, "Physical erasure deleted an unrelated highlight")
            result["checks"].append("physical_partial_erase_preserves_other_highlights")
            page.evaluate("""() => {
              [...document.querySelectorAll('#paper-wrap .ls-text')].find(el=>el.textContent.length>20&&!el.querySelector('mark')).dataset.nativeEraseUnmarked='yes';
            }""")
            mouse.select_text(page,'#paper-wrap [data-native-erase-unmarked="yes"]', start=0, end=6)
            page.evaluate("async () => { await highlightWrite; }")
            require(client.get(f"http://127.0.0.1:{port}/api/reading-highlights?id={arxiv_id}").json()["highlights"] == erased,
                    "Physical erasure on unmarked text changed annotations")
            result["checks"].append("physical_unmarked_erase_is_noop")
            page.screenshot(path=str(run / "mouse-partial-erase.png"))
            page.locator("#btn-reader-close").click()
            page.locator(".note").first.click()
            page.locator("#paper-wrap mark").first.wait_for()
            require(client.get(f"http://127.0.0.1:{port}/api/reading-highlights?id={arxiv_id}").json()["highlights"] == erased,
                    "Erasure did not persist after reopening")
            require(not page.evaluate("eraseMode || hlMode"), "Reopening kept a destructive tool mode active")
            result["checks"].append("physical_erasure_reopened")
            page.locator("#btn-reader-pdf").click()
            page.wait_for_function("readerView === 'pdf' && pdfLoaded && !document.querySelector('#btn-hl').disabled")
            result["pdf_points"] = mouse.select_text(page,"#pdf-wrap .txtlayer span")
            result["pdf_input"] = page.evaluate("({view:readerView,mode:hlMode,selected:window.getSelection().toString(),events:window._mouseAudit.slice(-12)})")
            require(len(page.evaluate("window.getSelection().toString()")) >= 5, "Physical mouse could not select PDF comparison text")
            result["checks"].append("physical_pdf_text_selection")
            page.locator("#r-title").click()
            page.locator("#btn-hl").click()
            mouse.select_text(page,"#pdf-wrap .txtlayer span")
            page.evaluate("async () => { await highlightWrite; }")
            pdf_before = client.get(f"http://127.0.0.1:{port}/api/highlights?id={arxiv_id}").json()["highlights"]
            require(pdf_before, "Physical PDF highlight was not saved")
            page.locator("#btn-hl-clear").click()
            mouse.select_text(page,"#pdf-wrap .txtlayer span", start=4, end=10)
            page.evaluate("async () => { await highlightWrite; }")
            pdf_after = client.get(f"http://127.0.0.1:{port}/api/highlights?id={arxiv_id}").json()["highlights"]
            require(0 < sum(h["w"] * h["h"] for h in pdf_after) < sum(h["w"] * h["h"] for h in pdf_before),
                    "Physical PDF partial erasure did not retain unselected area")
            require(client.get(f"http://127.0.0.1:{port}/api/reading-highlights?id={arxiv_id}").json()["highlights"] == erased,
                    "PDF erasure changed the HTML marks")
            result["checks"].append("physical_partial_pdf_erase")
            require(not result["page_errors"], "Native WebView reported JavaScript errors")
            page.locator("#btn-reader-close").click()
            handle = page.locator("#titlebar .tb-drag").bounding_box()
            if handle is None:
                raise AssertionError("Titlebar drag handle missing")
            before = mouse.bounds()
            x, y = handle["x"] + 80, handle["y"] + handle["height"] / 2
            mouse.drag(page, [x,y], [x,y+24])
            after = mouse.bounds()
            require(abs(after[0]-before[0]) <= 2 and after[1] > before[1], "Titlebar drag regressed")
            result["checks"].append("titlebar_still_drags_vertically")
            result["passed"] = True
            page.evaluate("window.pywebview.api.quit_app()")
            process.wait(timeout=20)
            print(f"PASS: physical Windows selection, saved/reopened highlights, PDF text, body/titlebar drag boundaries; artifacts: {run}")
    except Exception as error:
        result["error"] = str(error)
        if page is not None:
            try:
                result["ui_state"] = page.evaluate("({view:readerView,source:paperState && paperState.source,message:document.querySelector('#app-message').textContent,progress:document.querySelector('#paper-source-info').textContent})")
                page.screenshot(path=str(run / "failure.png"))
            except Exception as diagnostic:
                result["diagnostic_error"] = str(diagnostic)
        raise
    finally:
        app._write_json(run / "result.json", result)
        if mouse is not None:
            mouse.restore()
        if process.poll() is None:
            try:
                if page is not None:
                    page.evaluate("window.pywebview.api.quit_app()")
                process.wait(timeout=10)
            except Exception:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        log.close()
        print(f"Mouse test result: {run / 'result.json'}", flush=True)


if __name__ == "__main__":
    main()
