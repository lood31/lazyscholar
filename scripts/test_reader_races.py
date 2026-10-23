"""Deterministic two-window annotation races and interrupted lazy-PDF regressions."""
from __future__ import annotations

import re

import test_app_backend as backend
from playwright.sync_api import sync_playwright


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def select(page, selector, start, end):
    page.evaluate("""({selector,start,end}) => {
      const el=document.querySelector(selector), walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);
      const points=[]; let offset=0;
      while(walker.nextNode()) { const node=walker.currentNode; points.push({node,start:offset,end:offset+node.textContent.length}); offset+=node.textContent.length; }
      const locate=position=>{ const found=points.find(p=>position<=p.end); if(!found) throw Error('Selection offset out of range'); return [found.node,position-found.start]; };
      const range=document.createRange(), first=locate(start), last=locate(end);
      range.setStart(...first); range.setEnd(...last);
      const selection=window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      document.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
    }""", {"selector": selector, "start": start, "end": end})


def open_reader(context, pdf=False):
    page = context.new_page()
    page.goto(backend.ApiTests.base + "/app")
    page.locator("[data-tab='notes']").click()
    page.locator(".note").filter(has_text="A real note").click()
    page.locator("#paper-pages math").first.wait_for()
    if pdf:
        page.locator("#btn-reader-pdf").click()
        page.wait_for_function("pdfLoaded && !document.querySelector('#btn-hl').disabled")
    else:
        page.wait_for_function("!document.querySelector('#btn-hl').disabled")
    return page


def races(first, second, context, pdf=False):
    endpoint = "/api/highlights" if pdf else "/api/reading-highlights"
    url = backend.ApiTests.base + endpoint + "?id=2510.16720"
    pattern = re.compile(re.escape(url) + "$")
    conflicts = []
    first.on("response", lambda response: conflicts.append(response.status) if response.url == url and response.status == 409 else None)
    if pdf:
        selector = '#pdf-wrap .pg[data-page="1"] .txtlayer span[data-race-text="true"]'
        for page in (first, second):
            page.evaluate("""() => {
              const el=[...document.querySelectorAll('#pdf-wrap .pg[data-page="1"] .txtlayer span')].find(e=>e.textContent.length>60);
              if(!el) throw Error('No long PDF span'); el.dataset.raceText='true';
            }""")
    else:
        block = first.evaluate("[...document.querySelectorAll('#paper-pages [data-block]')].find(e=>e.textContent.length>60).dataset.block")
        selector = f'#paper-pages [data-block="{block}"]'
    for page in (first, second):
        page.locator("#btn-hl").click()
    held = []

    def gate(route):
        if route.request.method == "POST" and not held:
            held.append(route)
        else:
            route.continue_()

    first.route(pattern, gate)
    with first.expect_request(lambda request: request.url == url and request.method == "POST"):
        select(first, selector, 2, 12)
    select(second, selector, 22, 32)
    second.evaluate("async () => { await highlightWrite; }")
    before = context.request.get(url).json()["highlights"]
    check(before, "Second window did not save its addition")
    check(len(held) == 1, "First writer was not paused before its stale save")
    stale_add = held[0].request.post_data_json["highlights"]
    held[0].continue_()
    first.evaluate("async () => { await highlightWrite; }")
    first.unroute(pattern, gate)
    added = context.request.get(url).json()["highlights"]
    check(all(row in added for row in before + stale_add), "Concurrent additions lost another window's marks")
    check(conflicts == [409], "Stale addition was not rejected and retried")

    first.locator("#btn-hl-clear").click()
    held.clear()
    first.route(pattern, gate)
    with first.expect_request(lambda request: request.url == url and request.method == "POST"):
        select(first, selector, 2, 12)  # Erase the first window's original addition.
    select(second, selector, 42, 52)  # Add another mark while the eraser save is paused.
    second.evaluate("async () => { await highlightWrite; }")
    newer = context.request.get(url).json()["highlights"]
    check(len(newer) > len(added), "Concurrent new mark was not saved")
    check(len(held) == 1, "Eraser write was not paused")
    held[0].continue_()
    first.evaluate("async () => { await highlightWrite; }")
    first.unroute(pattern, gate)
    erased = context.request.get(url).json()["highlights"]
    check(all(row in erased for row in newer if row not in stale_add), "Eraser retry deleted another window's additions")
    check(not any(row in erased for row in stale_add), "A concurrently erased mark was resurrected")
    check(conflicts == [409, 409], "Stale erasure did not use conflict/retry")


def lazy_switches(page, context):
    page.evaluate("""async () => {
      const document=await pdfDocumentPromise;
      const raster=await document.getPage(3), text=await document.getPage(4);
      const render=raster.render.bind(raster), getText=text.getTextContent.bind(text);
      raster.render=options=>{
        const gate=new Promise(resolve=>{window._releaseRaster=resolve;}); window._rasterStarted=true;
        return {promise:gate.then(()=>render(options).promise)};
      };
      text.getTextContent=async (...args)=>{
        const gate=new Promise(resolve=>{window._releaseText=resolve;}); window._textStarted=true;
        await gate; return getText(...args);
      };
    }""")
    third = page.locator('#pdf-wrap .pg[data-page="3"]')
    third.scroll_into_view_if_needed()
    page.wait_for_function("window._rasterStarted === true")
    page.locator("#btn-reader-note").click()
    page.evaluate("window._releaseRaster()")
    page.wait_for_function("document.querySelector('#pdf-wrap .pg[data-page=\"3\"]').dataset.rendered === 'true'")
    page.locator("#btn-reader-pdf").click()
    check(third.locator(".txtlayer span").count() > 0, "Hidden raster completion stranded page 3's text layer")
    fourth = page.locator('#pdf-wrap .pg[data-page="4"]')
    fourth.scroll_into_view_if_needed()
    page.wait_for_function("window._textStarted === true")
    page.locator("#btn-reader-paper").click()
    page.evaluate("window._releaseText()")
    page.wait_for_function("document.querySelector('#pdf-wrap .pg[data-page=\"4\"]').dataset.rendered === 'true'")
    page.locator("#btn-reader-pdf").click()
    check(fourth.locator(".txtlayer span").count() > 0, "Hidden text completion stranded page 4")
    page.locator("#btn-hl").click()
    selector = '#pdf-wrap .pg[data-page="4"] .txtlayer span[data-after-switch="true"]'
    page.evaluate("""() => {
      const el=[...document.querySelectorAll('#pdf-wrap .pg[data-page="4"] .txtlayer span')].find(e=>e.textContent.length>30);
      if(!el) throw Error('Page 4 text is not selectable'); el.dataset.afterSwitch='true';
    }""")
    select(page, selector, 2, 12)
    page.evaluate("async () => { await highlightWrite; }")
    marks = context.request.get(backend.ApiTests.base + "/api/highlights?id=2510.16720").json()["highlights"]
    check(any(row["page"] == 4 for row in marks), "Lazy page could not save a highlight after view switching")


def main():
    backend.ApiTests.setUpClass()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=pw.chromium.executable_path)
            try:
                context = browser.new_context(viewport={"width": 900, "height": 850})
                errors = []
                context.on("page", lambda page: page.on("pageerror", lambda error: errors.append(str(error))))
                writer = context.new_page()
                paragraph = "<p>Selectable PDF regression text has enough characters for independent concurrent selections.</p>" * 8
                writer.set_content("<style>section{break-after:page}section:last-child{break-after:auto}</style>" +
                                   "".join(f"<section><h1>Page {number}</h1>{paragraph}</section>" for number in range(1, 5)))
                (backend.ApiTests.vault / "papers_pdf" / "2510.16720.pdf").write_bytes(writer.pdf(format="A4"))
                writer.close()
                first, second = open_reader(context), open_reader(context)
                races(first, second, context)
                first.close()
                second.close()
                first, second = open_reader(context, pdf=True), open_reader(context, pdf=True)
                races(first, second, context, pdf=True)
                lazy_switches(first, context)
                first.screenshot(path=str(backend.RUN / "reader-concurrency-and-lazy.png"))
                check(not errors, f"JavaScript errors: {errors}")
                print(f"PASS: HTML/PDF two-window add/erase races, stale-write retries, hidden raster/text completion; artifacts: {backend.RUN}")
            finally:
                browser.close()
    finally:
        backend.ApiTests.tearDownClass()


if __name__ == "__main__":
    main()
