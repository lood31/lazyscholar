"""Paper-card/queue trash UI, restore, cross-window closure and stale-request safety."""
from __future__ import annotations

import json

import test_app_backend as backend
from playwright.sync_api import sync_playwright

PAPER = "2510.16720"


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    backend.ApiTests.setUpClass()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=pw.chromium.executable_path)
            try:
                context = browser.new_context(viewport={"width": 700, "height": 850})
                errors = []
                pending = {}
                context.on("request", lambda request: pending.__setitem__(request, request.url))
                context.on("requestfinished", lambda request: pending.pop(request, None))
                context.on("requestfailed", lambda request: pending.pop(request, None))
                context.on("page", lambda page: page.on("pageerror", lambda error: errors.append(str(error))))
                request = context.request
                base, vault = backend.ApiTests.base, backend.ApiTests.vault
                note_path = vault / "papers" / "fixture.md"
                note_path.write_text(note_path.read_text(encoding="utf-8") + "\n\n- Paper ID: " + PAPER + "v2\n", encoding="utf-8")
                pdf = vault / "papers_pdf" / (PAPER + ".pdf")
                original_pdf = pdf.read_bytes()
                # This fixture path is an app cache; an additional external source must survive.
                external = vault / (PAPER + "v2.pdf")
                external.write_bytes(original_pdf)
                added = request.post(base + "/api/queue", data={"kind": "pdf", "value": str(external)})
                check(added.ok, "Cannot seed a queued copy")
                row = added.json()["item"]
                marks_url = base + "/api/highlights?id=" + PAPER
                revision = request.get(marks_url).json()["revision"]
                mark = {"page": 1, "x": .1, "y": .2, "w": .3, "h": .04}
                response = request.post(marks_url, data={"revision": revision, "highlights": [mark]})
                check(response.ok, "Cannot seed a saved annotation")
                page = context.new_page()
                page.goto(base + "/app")
                page.locator("[data-tab='notes']").click()
                page.locator(".note").filter(has_text="A real note").click()
                page.locator("#paper-pages math").first.wait_for()
                with context.expect_page() as opened:
                    page.locator("#btn-reader-expand").click()
                reader = opened.value
                reader.locator("#paper-pages math").first.wait_for()
                check(reader.evaluate("curArxiv") == PAPER, "Versioned note metadata split the reader identity")
                page.locator("#btn-reader-close").click()
                # Cancelling confirmation must not alter any files or queue entries.
                page.once("dialog", lambda dialog: dialog.dismiss())
                page.locator(".note").filter(has_text="A real note").locator("[data-trash-note]").click()
                check(pdf.exists(), "Cancelled confirmation moved the PDF")
                page.once("dialog", lambda dialog: dialog.accept())
                with page.expect_response(lambda response: response.url.endswith("/api/papers/trash")) as moved:
                    page.locator(".note").filter(has_text="A real note").locator("[data-trash-note]").click()
                check(moved.value.ok, "Trash request failed: " + str(moved.value.status) + " " + moved.value.text()
                      + " pending=" + json.dumps(list(pending.values())))
                if not reader.is_closed():
                    try:
                        reader.wait_for_event("close", timeout=5000)
                    except Exception:
                        print("READER_CLOSE_FAILURE", reader.evaluate("({id:curArxiv,standalone:standaloneReader,channel:!!paperEvents})"), errors)
                        raise
                page.locator(".note").filter(has_text="A real note").wait_for(state="detached")
                check(not pdf.exists(), "Deleting a paper left its app PDF in place")
                check(external.read_bytes() == original_pdf, "Deleting modified the external original")
                check(all(item["id"] != row["id"] for item in request.get(base + "/api/queue").json()), "Queue item survived trashing")
                # An old tab or delayed request cannot recreate cache/annotation files.
                for url in ("/api/pdf?id=", "/api/paper?id=", "/api/paper?refresh=1&id=", "/api/reading-highlights?id=", "/api/highlights?id="):
                    for identity in (PAPER, PAPER + "v2"):
                        check(request.get(base + url + identity).status == 410, "Deleted paper could be fetched: " + url + identity)
                check(request.post(marks_url, data={"revision": revision, "highlights": []}).status == 410,
                      "A stale annotation write recreated a deleted paper")
                check(request.post(base + "/api/queue", data={"kind": "pdf", "value": str(external)}).status == 400,
                      "Reimport bypassed the trash tombstone")
                check(not pdf.exists(), "A stale request recreated the PDF cache")
                page.locator("#btn-paper-trash").click()
                page.locator("[data-restore]").wait_for()
                entry = request.get(base + "/api/papers/trash").json()[0]
                check(entry["paper_id"] == PAPER, "Wrong paper in trash")
                page.screenshot(path=str(backend.RUN / "paper-trash.png"))
                page.locator("[data-restore]").click()
                page.locator("[data-restore]").wait_for(state="detached")
                check(pdf.read_bytes() == original_pdf, "Restore changed the PDF bytes")
                check(request.get(base + "/api/pdf?id=" + PAPER + "v2").body() == original_pdf, "Versioned URL did not use the canonical restored PDF")
                check(not (vault / "papers_pdf" / (PAPER + "v2.pdf")).exists(), "Versioned URL created a split cache")
                check(request.get(marks_url).json()["highlights"] == [mark], "Restore lost saved annotations")
                check(external.read_bytes() == original_pdf, "Restore modified the external original")
                page.locator("#btn-paper-trash").click()
                page.locator(".note").filter(has_text="A real note").wait_for()
                # Queue uses the same reversible whole-paper operation.
                page.locator("[data-tab='queue']").click()
                page.once("dialog", lambda dialog: dialog.accept())
                page.locator(f'[data-del="{row["id"]}"]').click()
                page.locator(f'[data-del="{row["id"]}"]').wait_for(state="detached")
                entries = request.get(base + "/api/papers/trash").json()
                check(len(entries) == 1, "Queue delete did not create one recoverable paper entry")
                check(request.post(base + "/api/papers/restore", data={"id": entries[0]["id"]}).ok, "Cannot restore queue deletion")
                check(pdf.read_bytes() == original_pdf, "Queue restore changed the PDF")
                check(request.post(base + "/api/papers/trash", data={"name": "../config.json"}).status == 400,
                      "Paper deletion accepted traversal")
                check(not errors, "Browser errors: " + str(errors))
                print("PASS: whole-paper trash/restore, cancellation, external originals, marks, queue, cross-window closure and stale-write rejection; artifacts: " + str(backend.RUN))
                (backend.RUN / "paper-trash-browser.json").write_text(json.dumps({"result": "PASS", "entry": entry}), encoding="utf-8")
            finally:
                browser.close()
    finally:
        backend.ApiTests.tearDownClass()


if __name__ == "__main__":
    main()
