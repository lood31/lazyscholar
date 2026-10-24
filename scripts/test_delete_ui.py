"""Unified single-paper selection and compact dialog; cancellation never recycles."""
from __future__ import annotations

import json

import test_app_backend as backend
from playwright.sync_api import sync_playwright


def check(value, message):
    if not value:
        raise AssertionError(message)


def main():
    backend.ApiTests.setUpClass()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=pw.chromium.executable_path)
            try:
                page = browser.new_page(viewport={"width": 404, "height": 720})
                errors, native_dialogs, deletions = [], [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                def reject_native_dialog(dialog):
                    native_dialogs.append(dialog.type)
                    dialog.dismiss()
                page.on("dialog", reject_native_dialog)
                page.on("request", lambda request: deletions.append(request.url) if request.url.endswith("/api/papers/delete") else None)
                page.goto(backend.ApiTests.base + "/app")
                page.locator("[data-tab='notes']").click()
                page.locator(".note").filter(has_text="A real note").wait_for()
                check(page.locator("[data-trash-note],#btn-paper-trash,#nlist button").count() == 0, "Per-card buttons or private trash UI remain")
                check(page.locator("#delete-notes").is_visible(), "Unified delete action missing")
                for width in (280, 404, 700):
                    page.set_viewport_size({"width": width, "height": 720})
                    for language in ("zh", "en"):
                        page.evaluate("language => applyLang(language)", language)
                        page.locator("#delete-notes").click()
                        check(page.locator("#delete-notes-selected").is_disabled(), "Delete is enabled without selection")
                        page.locator(".note").filter(has_text="A real note").click()
                        check(page.locator("[data-select-note]:checked").count() == 1, "Selection is not single-paper")
                        check(not page.locator("#reader").evaluate("node => node.classList.contains('open')"), "Selecting opened the reader")
                        page.locator("#delete-notes-selected").click()
                        page.locator("#paper-action-dialog").wait_for(state="visible")
                        metrics = page.locator("#paper-action-dialog").evaluate("""node => {
                          const box=node.getBoundingClientRect(), buttons=[...node.querySelectorAll('.paper-action-buttons button')].map(button=>{
                            const r=button.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom};});
                          return {width:box.width,height:box.height,left:box.left,right:box.right,bottom:box.bottom,top:box.top,
                            viewport:innerWidth,scroll:node.scrollWidth,client:node.clientWidth,buttons};
                        }""")
                        check(metrics["width"] <= min(340, width-32) + 1, "Dialog is too wide")
                        check(metrics["scroll"] <= metrics["client"]+1, "Dialog content overflows horizontally")
                        check(all(button["left"] >= metrics["left"] and button["right"] <= metrics["right"]
                                  and button["bottom"] <= metrics["bottom"] for button in metrics["buttons"]), "A dialog button is clipped")
                        page.keyboard.press("Escape")
                        page.locator("#paper-action-dialog").wait_for(state="hidden")
                        page.locator("#delete-notes-cancel").click()
                page.set_viewport_size({"width":404,"height":720})
                page.evaluate("applyLang('zh')")
                page.locator("#delete-notes").click()
                page.locator(".note").filter(has_text="A real note").click()
                page.screenshot(path=str(backend.RUN / "delete-selection.png"))
                page.locator("#delete-notes-selected").click()
                page.locator("#paper-action-dialog").wait_for(state="visible")
                page.screenshot(path=str(backend.RUN / "delete-compact-confirm.png"))
                page.locator("#paper-action-cancel").click()
                page.locator("#delete-notes-cancel").click()
                # Filtering away a selected card must clear it, never select the next row.
                page.locator("#delete-notes").click()
                page.locator(".note").filter(has_text="A real note").click()
                page.locator("#n-search").fill("no matching paper here")
                check(page.locator("#delete-notes-selected").is_disabled(), "Hidden selection was kept")
                page.locator("#delete-notes-cancel").click()
                # Long titles, dark mode and device scaling exercise the real dialog layout.
                long_note = backend.ApiTests.vault / "papers" / "long-dialog.md"
                long_note.write_text("# LONG-DIALOG-" + "长标题排版测试" * 16 + " X" * 100 + "\n\n- Paper ID: 2510.54321\n", encoding="utf-8")
                for scale in (1.25, 1.5, 2):
                    context = browser.new_context(viewport={"width":404,"height":720}, device_scale_factor=scale)
                    scaled = context.new_page()
                    scaled.on("pageerror", lambda error: errors.append(str(error)))
                    scaled.on("dialog", reject_native_dialog)
                    scaled.goto(backend.ApiTests.base + "/app")
                    scaled.locator("[data-tab='notes']").click()
                    scaled.locator(".note").filter(has_text="LONG-DIALOG-").wait_for()
                    for dark in (False, True):
                        scaled.evaluate("dark => {document.body.classList.toggle('dark',dark); applyLang('en');}", dark)
                        scaled.locator("#delete-notes").click()
                        scaled.locator(".note").filter(has_text="LONG-DIALOG-").click()
                        scaled.locator("#delete-notes-selected").click()
                        scaled.locator("#paper-action-dialog").wait_for(state="visible")
                        check(scaled.locator("#paper-action-dialog").evaluate("node => node.scrollWidth <= node.clientWidth + 1"), "Scaled long-title dialog overflows")
                        check(scaled.locator("#paper-action-confirm").evaluate("node => {const r=node.getBoundingClientRect(),d=node.closest('dialog').getBoundingClientRect();return r.top>=d.top && r.bottom<=d.bottom && r.left>=d.left && r.right<=d.right;}"), "Scaled confirm button is clipped")
                        if dark and scale == 1.5:
                            scaled.screenshot(path=str(backend.RUN / "delete-dark-scaled-confirm.png"))
                        scaled.locator("#paper-action-cancel").click()
                        scaled.locator("#delete-notes-cancel").click()
                    context.close()
                check(not deletions, "Cancellation invoked native recycling")
                check(not native_dialogs, "Browser-native confirm was invoked")
                check(not errors, "Browser errors: " + str(errors))
                result = {"result":"PASS","widths":[280,404,700],"languages":["zh","en"],"scales":[1.25,1.5,2],"themes":["light","dark"],"native_dialogs":native_dialogs,"delete_calls":deletions}
                (backend.RUN / "delete-ui-result.json").write_text(json.dumps(result),encoding="utf-8")
                print("PASS: unified single selection, compact bilingual dialog, no native confirm and no cancellation side effects; artifacts: " + str(backend.RUN))
            finally:
                browser.close()
    finally:
        backend.ApiTests.tearDownClass()


if __name__ == "__main__":
    main()
