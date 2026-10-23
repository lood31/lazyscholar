"""PDF zoom clicks must apply even while later-page metadata is still loading."""
from __future__ import annotations

import json

import test_app_backend as backend
from playwright.sync_api import sync_playwright


def main():
    backend.ApiTests.setUpClass()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=pw.chromium.executable_path)
            try:
                writer = browser.new_page()
                writer.set_content("<style>section{break-after:page}</style>" + "".join(
                    f"<section><h1>Page {n}</h1><p>PDF zoom should respond before all page metadata has arrived.</p></section>"
                    for n in range(1, 5)))
                (backend.ApiTests.vault / "papers_pdf" / "2510.16720.pdf").write_bytes(writer.pdf(format="A4"))
                writer.close()
                page = browser.new_page(viewport={"width": 404, "height": 642})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(backend.ApiTests.base + "/app")
                page.locator("[data-tab='notes']").click()
                page.locator(".note").filter(has_text="A real note").click()
                page.locator("#paper-pages math").first.wait_for()
                page.evaluate("""() => {
                  const original=pdfjsLib;
                  const gate=new Promise(resolve=>{window._releaseMetadata=resolve;});
                  window.pdfjsLib={...original,getDocument:(...args)=>({promise:original.getDocument(...args).promise.then(document=>{
                    const getPage=document.getPage.bind(document);
                    document.getPage=async number=>{if(number===2){window._metadataHeld=true;await gate;}return getPage(number);};
                    return document;
                  })})};
                }""")
                page.locator("#btn-reader-pdf").click()
                page.wait_for_function("window._metadataHeld === true")
                before = page.evaluate("({loaded:pdfLoaded,zoom:pdfZoom,width:document.querySelector('#pdf-wrap canvas').getBoundingClientRect().width})")
                if before["loaded"]:
                    raise AssertionError("Regression fixture did not hold the PDF loading state")
                try:
                    page.locator("#pdf-zoom-in").click()
                    page.wait_for_function("pdfZoom === 1.75 && document.querySelector('#pdf-wrap canvas')?.getBoundingClientRect().width > 1000", timeout=5000)
                    page.locator("#pdf-zoom-in").click()
                    page.wait_for_function("pdfZoom === 2 && document.querySelector('#pdf-wrap canvas')?.getBoundingClientRect().width > 1150", timeout=5000)
                except Exception:
                    state = page.evaluate("({loaded:pdfLoaded,zoom:pdfZoom,percent:document.querySelector('#pdf-scale').textContent})")
                    print("ZOOM_DURING_LOAD_FAILURE", json.dumps(state))
                    raise
                finally:
                    page.evaluate("window._releaseMetadata()")
                page.wait_for_function("pdfLoaded && pdfZoom === 2")
                after = page.evaluate("({zoom:pdfZoom,width:document.querySelector('#pdf-wrap canvas').getBoundingClientRect().width})")
                if after["width"] <= before["width"] * 1.25:
                    raise AssertionError("Queued zoom changed its label but not the actual PDF size")
                if errors:
                    raise AssertionError(str(errors))
                page.screenshot(path=str(backend.RUN / "pdf-loading-zoom.png"))
                print(f"PASS: zoom during loading and repeated clicks apply to actual PDF pages; artifacts: {backend.RUN}")
            finally:
                browser.close()
    finally:
        backend.ApiTests.tearDownClass()


if __name__ == "__main__":
    main()
