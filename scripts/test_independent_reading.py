"""Offline browser regression: independent reading, PDF zoom and shared marks."""
from __future__ import annotations

from playwright.sync_api import sync_playwright
from test_app_backend import RUN, ApiTests, app


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    from html_fixture import seed_html

    ApiTests.setUpClass()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=pw.chromium.executable_path)
            try:
                context = browser.new_context(viewport={"width": 404, "height": 642}, device_scale_factor=2)
                errors = []
                generation_requests = []
                context.on("page", lambda p: p.on("pageerror", lambda e: errors.append(str(e))))
                context.on("request", lambda r: generation_requests.append(r.url) if "/api/queue/run" in r.url else None)
                writer = context.new_page()
                paragraphs = "<p>Original local PDF text remains readable without a model or metadata request.</p>" * 12
                writer.set_content("<style>section{break-after:page}section:last-child{break-after:auto}</style>" +
                                   "".join(f"<section><h1>Local PDF page {n}</h1>{paragraphs}</section>" for n in range(1, 5)))
                pdf = writer.pdf(format="A4")
                writer.close()
                paper_id = "2510.00007"
                source = ApiTests.vault / f"{paper_id}.pdf"
                source.write_bytes(pdf)
                seed_html(ApiTests.vault, paper_id)
                response = context.request.post(ApiTests.base + "/api/queue", data={"kind": "pdf", "value": str(source)})
                check(response.ok, "Cannot import the pending local PDF")
                item_id = response.json()["item"]["id"]
                page = context.new_page()
                page.goto(ApiTests.base + "/app")
                page.locator(f'[data-read="{item_id}"]').click()
                page.wait_for_function("pdfLoaded && readerView === 'pdf'")
                check(page.locator("#r-title").inner_text() == source.name, "Pending reader lost the source name")
                check(page.locator("#pdf-wrap .pg").count() >= 4, "PDF page placeholders are missing")
                check(page.locator("#pdf-wrap canvas").count() < page.locator("#pdf-wrap .pg").count(), "All PDF pages were rasterized eagerly")
                check(page.locator("#pdf-scale").inner_text() == "150%", "Default PDF text was shrunk to the narrow panel")
                dimensions = page.locator("#pdf-wrap canvas").first.evaluate("el => ({raster:el.width,css:el.getBoundingClientRect().width})")
                check(dimensions["raster"] > dimensions["css"] * 1.5, "High-DPI PDF rendering is missing")
                check(page.locator("#pdf-wrap").evaluate("el => el.scrollWidth > el.clientWidth"), "Zoomed PDF cannot pan horizontally")
                check(page.locator("#reader-font-up").is_hidden(), "HTML font controls misleadingly remain visible for PDF")
                page.locator("#btn-reader-note").click()
                check("尚未生成导读" in page.locator("#note-md").inner_text(), "A missing note did not have an explicit reading-independent state")
                page.locator("#btn-reader-pdf").click()
                page.wait_for_function("pdfLoaded && !document.querySelector('#btn-hl').disabled")
                page.locator("#btn-hl").click()
                page.evaluate("""() => {
                  const span = [...document.querySelectorAll('.txtlayer span')].find(el => el.textContent.length > 25);
                  const range = document.createRange(); range.setStart(span.firstChild,0); range.setEnd(span.firstChild,20);
                  const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
                  document.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
                }""")
                page.locator("#pdf-wrap .hl").first.wait_for()
                marks_url = ApiTests.base + "/api/highlights?id=" + paper_id
                marks = context.request.get(marks_url).json()["highlights"]
                check(marks, "Pending PDF highlights were not saved")
                page.locator("#pdf-zoom-in").click()
                page.wait_for_function("pdfLoaded && document.querySelector('#pdf-scale').textContent === '175%'")
                check(page.locator("#pdf-wrap .hl").count() == len(marks), "Zooming lost PDF highlights")
                check(context.request.get(marks_url).json()["highlights"] == marks, "Zoom changed saved PDF coordinates")
                page.locator("#pdf-fit").click()
                page.wait_for_function("pdfLoaded && pdfZoom === null")
                fitted = page.locator("#pdf-wrap .pg").first.evaluate("el => el.getBoundingClientRect().width")
                check(fitted < 404, "Fit-width mode did not fit the panel")
                page.locator("#pdf-zoom-in").click()
                page.wait_for_function("pdfLoaded && pdfZoom !== null")
                page.locator("#btn-reader-paper").click()
                page.locator("#paper-pages math").first.wait_for()
                page.locator("#btn-reader-pdf").click()
                page.wait_for_function("pdfLoaded")
                later = page.locator('#pdf-wrap .pg[data-page="3"]')
                later.scroll_into_view_if_needed()
                page.wait_for_function("document.querySelector('#pdf-wrap .pg[data-page=\"3\"]').dataset.rendered === 'true'")
                check(later.locator(".txtlayer span").count() > 0, "Lazy PDF pages stopped loading after switching views")
                page.screenshot(path=str(RUN / "independent-pdf-zoom.png"))
                with context.expect_page() as opened:
                    page.locator("#btn-reader-expand").click()
                expanded = opened.value
                expanded.set_viewport_size({"width": 1100, "height": 850})
                expanded.wait_for_function("pdfLoaded && standaloneReader")
                check(expanded.locator("#btn-reader-expand").is_hidden(), "Standalone reader still offers recursive expansion")
                check(expanded.locator("main").is_hidden(), "Standalone reader exposed the queue behind the reader")
                check(expanded.locator("#pdf-wrap .hl").count() == len(marks), "Expanded reader did not share existing highlights")
                expanded.screenshot(path=str(RUN / "independent-pdf-expanded.png"))
                with expanded.expect_event("close"):
                    expanded.locator("#btn-reader-close").click()
                # A failed generation must leave the same local file and marks readable.
                app.update_queue_item(ApiTests.vault, item_id, status="failed", error="Model unavailable")
                page.locator("#btn-reader-close").click()
                page.evaluate("refreshQueue()")
                page.locator(f'[data-read="{item_id}"]').click()
                page.wait_for_function("pdfLoaded")
                check(page.locator("#pdf-wrap .hl").count() == len(marks), "Failed-item reading lost saved marks")
                check(source.read_bytes() == pdf, "Reading modified the original PDF")
                check(not list((ApiTests.vault / "papers").glob(paper_id + "*.md")), "Reading generated a fake note")
                item = next(i for i in context.request.get(ApiTests.base + "/api/queue").json() if i["id"] == item_id)
                check(item["status"] == "failed", "Reading changed generation status")
                # Existing done notes still work from the queue and retain PDF marks.
                (ApiTests.vault / "papers" / "local-note.md").write_text("# Generated later\n\narXiv: " + paper_id, encoding="utf-8")
                app.update_queue_item(ApiTests.vault, item_id, status="done", note="local-note.md", error=None)
                page.locator("#btn-reader-close").click()
                page.evaluate("refreshQueue()")
                page.locator(f'[data-read="{item_id}"]').click()
                page.wait_for_function("pdfLoaded")
                page.locator("#btn-reader-note").click()
                check("Generated later" in page.locator("#note-md").inner_text(), "A later generated note did not become available")
                # Links can enter HTML reading before a note exists too.
                link_id = "2510.00008"
                seed_html(ApiTests.vault, link_id)
                link = context.request.post(ApiTests.base + "/api/queue", data={"kind": "link", "value": link_id}).json()["item"]
                page.locator("#btn-reader-close").click()
                page.evaluate("refreshQueue()")
                page.locator(f'[data-read="{link["id"]}"]').click()
                page.locator("#paper-pages math").first.wait_for()
                check(page.evaluate("curNote === null && readerView === 'paper'"), "A pending link still depends on a generated note")
                check(not generation_requests, f"Reading unexpectedly started generation: {generation_requests}")
                check(not errors, f"JavaScript errors: {errors}")
                print(f"PASS: pending/failed reading, no generation, high-DPI PDF zoom/pan/fit, lazy-page resume, shared marks, standalone reader; artifacts: {RUN}")
            finally:
                browser.close()
    finally:
        ApiTests.tearDownClass()


if __name__ == "__main__":
    main()
