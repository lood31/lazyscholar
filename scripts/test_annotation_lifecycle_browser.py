"""Deferred annotation and receipt-failure browser regressions; fake OS only."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import test_app_backend as backend
from playwright.sync_api import sync_playwright
from test_highlight_eraser import select
from test_paper_trash_browser import recover

PAPER = "2510.16720"
RUN = backend.RUN
EXE: Path | None = None


def check(condition, message):
    if not condition:
        raise AssertionError(message)


@contextmanager
def server(name, fail_receipt=False):
    backend.RUN = RUN / name
    original = subprocess.Popen
    bootstrap = '''import sys
from pathlib import Path
sys.path.insert(0,str(Path('scripts').resolve()))
import test_app_backend as b
from test_paper_trash import FakeBin
import windows_recycle, paper_trash
v=Path(sys.argv[1]);provider=FakeBin(v.parent/'fake-bin')
windows_recycle.recycle=provider.recycle
save=paper_trash._save_native
def injected_save(vault, rows):
    if sys.argv[2]=='fail' and any(row.get('completed') for row in rows.values()):
        raise OSError('simulated receipt storage failure after confirmed recycle')
    return save(vault,rows)
paper_trash._save_native=injected_save
b.app.serve_ui(v,open_browser=False)
'''
    def launch(command, **kwargs):
        if EXE is not None and not fail_receipt:
            return original([str(EXE),"--ui","--vault",str(backend.ApiTests.vault)],**kwargs)
        return original([sys.executable,"-B","-c",bootstrap,str(backend.ApiTests.vault),"fail" if fail_receipt else "normal"],**kwargs)
    with patch.object(backend.subprocess,"Popen",side_effect=launch):
        backend.ApiTests.setUpClass()
    try:
        yield backend.ApiTests
    finally:
        if EXE is not None and not fail_receipt and backend.ApiTests.proc.poll() is None:
            subprocess.run(["taskkill.exe","/PID",str(backend.ApiTests.proc.pid),"/T","/F"],capture_output=True,check=False)
            backend.ApiTests.proc.wait(timeout=15)
        backend.ApiTests.tearDownClass()
        backend.RUN = RUN


def ready(page, view):
    if view == "pdf":
        page.locator("#btn-reader-pdf").click()
        page.wait_for_function("pdfLoaded && document.querySelector('#pdf-wrap .txtlayer span')")
    else:
        page.locator("#paper-pages [data-block]").first.wait_for()
        page.wait_for_function("paperState && !paperState.loading && !!paperState.source")
    page.wait_for_function("!document.querySelector('#btn-hl').disabled")


def deferred_case(browser, view, mode="restore"):
    with server("queued-"+view+"-"+mode) as fixture:
        context = browser.new_context(viewport={"width":700,"height":850})
        record = None
        try:
            page = context.new_page()
            page.goto(fixture.base+"/app")
            page.locator("[data-tab='notes']").click()
            page.locator(".note").filter(has_text="A real note").click()
            ready(page,view)
            page.locator("#btn-hl").click()
            old_epoch = page.evaluate("curLifecycle")
            page.evaluate("() => {highlightWrite=new Promise(resolve=>{window.releaseOldWrite=resolve;});}")
            selector = "#pdf-wrap .txtlayer span" if view == "pdf" else "#paper-pages [data-block]"
            page.evaluate("selector => {const el=[...document.querySelectorAll(selector)].find(e=>e.textContent.length>=6);if(!el)throw Error('missing fixture text');el.id='old-selection';}",selector)
            select(page,"#old-selection",0,"#old-selection",5,wait=False)
            page.evaluate("() => {window.oldOutcome=highlightWrite.then(()=>({ok:true}),error=>({error:error.message}));}")
            request = context.request
            original = fixture.vault / (PAPER+"v2.pdf")
            original.write_bytes((fixture.vault/"papers_pdf"/(PAPER+".pdf")).read_bytes())
            preview = request.post(fixture.base+"/api/papers/preview",data={"name":"fixture.md"}).json()
            deleted = request.post(fixture.base+"/api/papers/delete",data={"name":"fixture.md","token":preview["token"]})
            record = backend.app.paper_trash._native(fixture.vault).get(PAPER)
            check(deleted.ok,"Cannot prepare deleted lifecycle")
            if record is None:
                raise AssertionError("Missing deletion receipt")
            page.locator("#btn-reader-close").click()
            if mode == "restore":
                recover(fixture.vault,record,EXE is not None)
                record = None
                page.evaluate("refreshNotes()")
                page.locator(".note").filter(has_text="A real note").click()
            else:
                imported = request.post(fixture.base+"/api/queue",data={"kind":"pdf","value":str(original),"paper_id":PAPER,"reactivate":record["id"]})
                check(imported.ok,"Explicit fixture reimport failed")
                item = imported.json()["item"]
                page.locator("[data-tab='queue']").click()
                page.locator('[data-read="'+item["id"]+'"]').click()
            # Rendering waits for highlightWrite, but the reader identity changes first.
            page.wait_for_function("old => curArxiv === '2510.16720' && curLifecycle !== old",arg=old_epoch)
            stale_requests = []
            page.on("request",lambda req:stale_requests.append(req.method) if req.method == 'POST' and "highlights?" in req.url else None)
            page.evaluate("""() => {
                window.oldMutationReads=0;const original=api;
                api=(...args)=>{if(new Error().stack.includes('mutateHighlights'))window.oldMutationReads++;return original(...args);};
                window.releaseOldWrite();
            }""")
            outcome = page.evaluate("() => window.oldOutcome")
            check(not outcome.get("ok"),"Queued old selection borrowed the reopened reader's epoch")
            check(not stale_requests,"Old queued selection submitted against the new reader: "+str(stale_requests))
            check(page.evaluate("oldMutationReads") == 0,"Stale mutation fetched the fresh revision")
            endpoint = "/api/highlights?id=" if view == "pdf" else "/api/reading-highlights?id="
            check(request.get(fixture.base+endpoint+PAPER,headers={"X-Paper-Lifecycle":page.evaluate("curLifecycle")}).json()["highlights"] == [],"Stale selection mutated restored annotations")
            ready(page,view)
            page.locator("#btn-hl").click()
            page.evaluate("selector => {const el=[...document.querySelectorAll(selector)].find(e=>e.textContent.length>=6);el.id='fresh-selection';}",selector)
            select(page,"#fresh-selection",0,"#fresh-selection",5)
            check(request.get(fixture.base+endpoint+PAPER,headers={"X-Paper-Lifecycle":page.evaluate("curLifecycle")}).json()["highlights"],"Fresh reader could not save after rejecting stale work")
            print("PASS: queued "+view+" selection rejected before revision fetch after "+mode+"; fresh save works",flush=True)
        finally:
            context.close()
            if EXE is not None and record and record.get("locations"):
                for relative in record["locations"]:
                    target = fixture.vault / relative
                    if target.exists():
                        saved = fixture.vault.parent / ("preserved-new-"+record["id"]) / relative
                        saved.parent.mkdir(parents=True,exist_ok=True)
                        target.rename(saved)
                recover(fixture.vault,record,True)


def receipt_case(browser):
    with server("receipt-failure",True) as fixture:
        context = browser.new_context(viewport={"width":700,"height":850})
        try:
            page = context.new_page()
            page.goto(fixture.base+"/app")
            page.locator("[data-tab='notes']").click()
            page.locator(".note").filter(has_text="A real note").click()
            ready(page,"paper")
            with context.expect_page() as opened:
                page.locator("#btn-reader-expand").click()
            reader = opened.value
            reader.locator("#paper-pages [data-block]").first.wait_for()
            page.locator("#btn-reader-close").click()
            page.locator("#delete-notes").click()
            page.locator(".note").filter(has_text="A real note").click()
            page.locator("#delete-notes-selected").click()
            page.locator("#paper-action-dialog").wait_for(state="visible")
            with page.expect_response(lambda response:response.url.endswith("/api/papers/delete")) as response:
                page.locator("#paper-action-confirm").click()
            result = response.value.json()
            check(result.get("partial") and result.get("paper_id") == PAPER and result.get("completed",0)>0,"Confirmed recycle lost structured partial-success report: "+str(result))
            check(result.get("restore_required"),"Unpersisted system results incorrectly offer blind retry")
            if not reader.is_closed():
                reader.wait_for_event("close",timeout=5000)
            page.locator("#paper-action-dialog").wait_for(state="visible")
            check(not (fixture.vault/"papers"/"fixture.md").exists(),"Fixture was not physically recycled")
            check(context.request.get(fixture.base+"/api/pdf?id="+PAPER).status == 410,"Failed receipt allowed stale reader access")
            print("PASS: post-recycle receipt failure reports counts, closes old reader and requires restore",flush=True)
        finally:
            context.close()


def main():
    global EXE
    parser = argparse.ArgumentParser()
    parser.add_argument("--view",choices=["paper","pdf","all","receipt"],default="all")
    parser.add_argument("--exe",type=Path,help="Opt-in: real Windows recycling of isolated fixtures using this EXE")
    args=parser.parse_args()
    EXE=args.exe.resolve() if args.exe else None
    if EXE is not None and not EXE.is_file():
        raise ValueError("Missing packaged EXE")
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,executable_path=pw.chromium.executable_path)
        try:
            for view in ("paper","pdf"):
                if args.view in (view,"all"):
                    deferred_case(browser,view)
            if args.view == "all":
                deferred_case(browser,"pdf","reimport")
            if args.view in ("all","receipt"):
                receipt_case(browser)
            (RUN/"annotation-lifecycle-result.json").write_text(json.dumps({"result":"PASS","view":args.view,"exe":str(EXE) if EXE else None}),encoding="utf-8")
            print("Artifacts: "+str(RUN))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
