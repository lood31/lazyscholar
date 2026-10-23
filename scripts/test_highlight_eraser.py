"""Selection-scoped erasing: fragments, no-ops, queued modes and PDF geometry.

Browser selections exercise the UI/storage contract. Physical Windows input is
covered separately by test_native_highlight.py, not claimed by this test.
"""
from __future__ import annotations

import math

from playwright.sync_api import sync_playwright
from test_app_backend import RUN, ApiTests


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def select(page, first, start, last, end, wait=True):
    result = page.evaluate("""args => {
      function point(selector, offset){
        const el=document.querySelector(selector), walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);
        let node;
        while ((node=walker.nextNode())) {
          if(offset<=node.textContent.length) return [node,offset];
          offset-=node.textContent.length;
        }
        throw Error('Selection offset is outside the text');
      }
      const a=point(args.first,args.start),b=point(args.last,args.end),range=document.createRange();
      range.setStart(...a);range.setEnd(...b);
      const pg=(a[0].parentElement).closest('.pg'),rects=[];
      if(pg){const p=pg.getBoundingClientRect();for(const r of range.getClientRects())
        if(r.width>=2&&r.height>=2)rects.push({page:Number(pg.dataset.page),x:(r.left-p.left)/p.width,y:(r.top-p.top)/p.height,w:r.width/p.width,h:r.height/p.height});}
      const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);
      document.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
      return rects;
    }""", {"first": first, "start": start, "last": last, "end": end})
    if wait:
        page.evaluate("async () => { await highlightWrite; }")
    return result


def main():
    ApiTests.setUpClass()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=pw.chromium.executable_path)
            try:
                writer = browser.new_page()
                writer.set_content('<h1>First PDF paragraph for selection and partial erasing</h1>' +
                                   '<p>Another unmarked PDF text paragraph on a separate line for testing.</p>' * 3 +
                                   '<section style="break-before:page"><h1>Second PDF page with an unrelated annotation</h1></section>')
                (ApiTests.vault / "papers_pdf" / "2510.16720.pdf").write_bytes(writer.pdf(format="A4"))
                writer.close()
                page = browser.new_page(viewport={"width": 440, "height": 800})
                errors, posts = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: posts.append(request.url) if request.method == "POST" and "/api/" in request.url else None)
                page.goto(ApiTests.base + "/app")
                page.locator("[data-tab='notes']").click()
                page.locator(".note").first.click()
                page.locator("#paper-pages [data-block]").first.wait_for()
                page.wait_for_function("!document.querySelector('#btn-hl').disabled")
                ids = page.evaluate("""() => {
                  const all=[...document.querySelectorAll('#paper-pages [data-block]')];
                  return [all[0].dataset.block,all.find(el=>el.closest('p')&&el.textContent.length>40).dataset.block];
                }""")
                a, b = (f'#paper-pages [data-block="{key}"]' for key in ids)
                text = page.locator(a).inner_text()
                url = ApiTests.base + "/api/reading-highlights?id=2510.16720"
                def marks():
                    return page.request.get(url).json()["highlights"]
                def ranges(block):
                    return sorted((m["start"], m["end"]) for m in marks() if m["block"] == block)

                page.locator("#btn-hl").click()
                select(page, a, 0, a, 24)
                select(page, b, 1, b, 9)
                before = marks()
                page.locator("#btn-hl-clear").click()
                check(marks() == before, "Entering eraser mode deleted annotations")
                check(page.evaluate("eraseMode && !hlMode"), "Modes are not mutually exclusive")
                select(page, a, 8, a, 16)
                check(ranges(ids[0]) == [(0, 8), (16, 24)], "Middle erasure did not preserve both ends")
                check(ranges(ids[1]) == [(1, 9)], "Erasure changed another paragraph")
                for row in marks():
                    if row["block"] == ids[0]:
                        check(row["quote"] == text[row["start"]:row["end"]], "Fragment quote no longer matches the text")
                select(page, a, 6, a, 18)
                check(ranges(ids[0]) == [(0, 6), (18, 24)], "Selection across a gap erased the wrong intervals")
                before, count = marks(), len(posts)
                select(page, a, 7, a, 17)
                check(marks() == before and len(posts) == count, "Unmarked selection was not a true no-op")
                page.locator("#btn-hl-clear").click()
                select(page, a, 0, a, 3)
                check(marks() == before, "Selection changed annotations after leaving eraser mode")
                page.locator("#btn-hl-clear").click()
                select(page, a, 0, a, 26)
                check(ranges(ids[0]) == [] and ranges(ids[1]) == [(1, 9)], "Selected erasure cleared unrelated marks")
                page.locator("#btn-reader-close").click()
                page.locator(".note").first.click()
                page.locator("#paper-pages mark").first.wait_for()
                check(not page.evaluate("eraseMode || hlMode"), "Reopening retained a destructive tool mode")
                page.locator("#reader-font-up").click()
                check(ranges(ids[1]) == [(1, 9)], "Font change lost the retained annotation")
                page.locator("#btn-hl-clear").click()
                select(page, b, 1, b, 9)
                check(marks() == [], "Selecting an entire remaining mark did not erase it")

                # The queued erase must stay an erase even when mode changes
                # before the preceding add request is released.
                stalled = []
                def delay_first_post(route):
                    if route.request.method == "POST" and not stalled:
                        stalled.append(route)
                    else:
                        route.continue_()
                page.route("**/api/reading-highlights?*", delay_first_post)
                page.locator("#btn-hl").click()
                with page.expect_request(lambda r: r.method == "POST" and "/api/reading-highlights?" in r.url):
                    select(page, a, 0, a, 20, wait=False)
                page.locator("#btn-hl-clear").click()
                select(page, a, 5, a, 10, wait=False)
                page.locator("#btn-hl").click()
                check(len(stalled) == 1, "Did not hold the add request")
                stalled[0].continue_()
                page.evaluate("async () => { await highlightWrite; }")
                page.unroute("**/api/reading-highlights?*", delay_first_post)
                check(ranges(ids[0]) == [(0, 5), (10, 20)], "Queued mode change turned an erase into an add")
                select(page, b, 1, b, 9)
                page.locator("#btn-hl-clear").click()
                select(page, a, 3, b, 5)
                check(ranges(ids[0]) == [(0, 3)] and ranges(ids[1]) == [(5, 9)], "Cross-paragraph selection erased outside its range")
                html_marks = marks()
                emoji = page.evaluate("eraseTextHighlights([{block:'emoji',start:1,end:7,quote:'A💡BCD'}],[{block:'emoji',start:2,end:4}])")
                check([(h["start"], h["end"], h["quote"]) for h in emoji] == [(1, 2, "A"), (4, 7, "BCD")], "UTF-16 fragment offsets split an emoji")

                parts = page.evaluate("erasePdfHighlights([{page:1,x:.1,y:.1,w:.6,h:.5},{page:2,x:.1,y:.1,w:.6,h:.5}],[{page:1,x:.3,y:.2,w:.2,h:.2}])")
                check(len(parts) == 5, "PDF rectangle subtraction did not retain four surrounding pieces and the other page")
                check(math.isclose(sum(h["w"] * h["h"] for h in parts if h["page"] == 1), .26), "PDF subtraction changed the wrong area")
                pdf_url = ApiTests.base + "/api/highlights?id=2510.16720"
                other = {"page": 2, "x": .1, "y": .1, "w": .2, "h": .02}
                response = page.request.post(pdf_url, data={"highlights": [other], "revision": page.request.get(pdf_url).json()["revision"]})
                check(response.ok, "Cannot seed the unrelated PDF annotation")
                page.locator("#btn-reader-pdf").click()
                page.wait_for_function("pdfLoaded && !document.querySelector('#btn-hl').disabled")
                check(not page.evaluate("eraseMode || hlMode"), "Switching views retained eraser mode")
                check(page.locator('#pdf-wrap .pg[data-page="2"]').count() == 1, "PDF test fixture is missing its second page")
                page.evaluate("""() => {
                  const all=[...document.querySelectorAll('#pdf-wrap .pg[data-page="1"] .txtlayer span')].filter(el=>el.textContent.length>24);
                  all[0].dataset.eraserTest='first';
                  all.find(el=>Math.abs(el.getBoundingClientRect().top-all[0].getBoundingClientRect().top)>10).dataset.eraserTest='other';
                }""")
                p = '#pdf-wrap [data-eraser-test="first"]'
                page.locator("#btn-hl").click()
                select(page, p, 0, p, 20)
                before = page.request.get(pdf_url).json()["highlights"]
                page.locator("#btn-hl-clear").click()
                check(page.request.get(pdf_url).json()["highlights"] == before, "PDF eraser button still clears the whole document")
                cuts = select(page, p, 6, p, 12)
                after = page.request.get(pdf_url).json()["highlights"]
                check([h for h in after if h["page"] == 2] == [other], "PDF erasure changed another page")
                def area(rows):
                    return sum(h["w"] * h["h"] for h in rows if h["page"] == 1)
                check(0 < area(after) < area(before), "PDF partial erasure did not retain the rest of the mark")
                for h in after:
                    for cut in cuts:
                        if h["page"] == cut["page"]:
                            overlap = max(0, min(h["x"] + h["w"], cut["x"] + cut["w"]) - max(h["x"], cut["x"])) * max(0, min(h["y"] + h["h"], cut["y"] + cut["h"]) - max(h["y"], cut["y"]))
                            check(overlap < 1e-12, "Erased PDF area remains highlighted")
                count = len(posts)
                other_span = '#pdf-wrap [data-eraser-test="other"]'
                select(page, other_span, 0, other_span, 10)
                check(page.request.get(pdf_url).json()["highlights"] == after and len(posts) == count, "Unmarked PDF text was changed")
                check(marks() == html_marks, "PDF erasure modified the HTML annotations")
                page.screenshot(path=str(RUN / "partial-eraser-pdf.png"))
                page.locator("#btn-reader-close").click()
                page.locator(".note").first.click()
                page.locator("#paper-pages mark").first.wait_for()
                page.locator("#btn-reader-pdf").click()
                page.wait_for_function("pdfLoaded && !document.querySelector('#btn-hl').disabled")
                check(page.locator("#pdf-wrap .hl").count() == len(after), "PDF fragments did not survive reopening")
                check(not errors, f"JavaScript errors: {errors}")
                print(f"PASS: selection-scoped HTML/PDF erasing, retained fragments, no-ops, reopen/font, UTF-16, and queued mode changes; artifacts: {RUN}")
            finally:
                browser.close()
    finally:
        ApiTests.tearDownClass()


if __name__ == "__main__":
    main()
