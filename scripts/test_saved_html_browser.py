"""Browser regression for inert saved HTML; optionally validate a real local snapshot."""
from __future__ import annotations

import argparse
from pathlib import Path

import test_app_backend as backend
from playwright.sync_api import sync_playwright
from test_saved_html import fixture, png


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=Path)
    args = parser.parse_args()
    backend.ApiTests.setUpClass()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=pw.chromium.executable_path)
            try:
                context = browser.new_context(viewport={"width": 900, "height": 850})
                errors, external = [], []
                context.on("page", lambda p: p.on("pageerror", lambda e: errors.append(str(e))))
                context.on("request", lambda r: external.append(r.url) if not r.url.startswith(backend.ApiTests.base) else None)
                source = backend.ApiTests.vault / "browser-saved.html"
                source.write_text(fixture().replace("</article>", "<img src='https://evil.example/missing.png'></article>"), encoding="utf-8")
                (source.parent / "figure.png").write_bytes(png())
                response = context.request.post(backend.ApiTests.base + "/api/queue", data={"kind": "html", "value": str(source)})
                check(response.ok, "Cannot import saved HTML")
                item = response.json()["item"]
                page = context.new_page()
                page.goto(backend.ApiTests.base + "/app")
                page.locator(f'[data-read="{item["id"]}"]').click()
                page.locator("#paper-pages math").first.wait_for()
                page.wait_for_function("!document.querySelector('#btn-hl').disabled")
                check(page.locator("#btn-reader-pdf").is_disabled(), "Saved HTML incorrectly offers an arXiv PDF")
                check(page.locator("#reader-font-up").is_visible(), "Saved HTML font controls are missing")
                check(page.evaluate("window.PAPER_SCRIPT_EXECUTED === undefined"), "Paper code executed in the app")
                notice = page.locator("#paper-source-info").inner_text()
                check("静态网页" in notice and "Canvas" in notice and "缺失" in notice, "Import limitations were hidden")
                check(page.locator("#paper-pages svg, #paper-pages canvas, #paper-pages script").count() == 0, "Active/unsupported elements entered the app DOM")
                check("DUPLICATE_FORMULA" not in page.locator("#paper-pages").inner_text(), "KaTeX visual duplicate was not removed")
                fraction = page.locator("#paper-pages mfrac").first.evaluate("el => [el.children[0].getBoundingClientRect().bottom,el.children[1].getBoundingClientRect().top]")
                check(fraction[0] <= fraction[1] + 1, "Saved MathML fraction was flattened")
                page.wait_for_function("[...document.querySelectorAll('#paper-pages img')].every(img => img.complete && img.naturalWidth > 0)")
                check(page.locator("#paper-pages img").count() == 2, "Saved raster/SVG images are missing")
                svg_url = page.locator("#paper-pages img").last.get_attribute("src")
                if svg_url is None:
                    raise AssertionError("SVG resource has no local URL")
                check("default-src 'none'" in context.request.get(backend.ApiTests.base + svg_url).headers.get("content-security-policy", ""), "SVG resource lacks its isolation policy")
                page.locator("#reader-font-up").click()
                page.locator("#btn-hl").click()
                page.evaluate("""() => {
                  const el=[...document.querySelectorAll('#paper-pages [data-block]')].find(e=>e.textContent.length>40);
                  const range=document.createRange(); range.setStart(el.firstChild,0); range.setEnd(el.firstChild,20);
                  const sel=window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
                  document.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
                }""")
                page.locator("#paper-pages mark").first.wait_for()
                page.locator("#btn-reader-close").click()
                page.locator(f'[data-read="{item["id"]}"]').click()
                page.locator("#paper-pages mark").first.wait_for()
                with context.expect_page() as opened:
                    page.locator("#btn-reader-expand").click()
                expanded = opened.value
                expanded.locator("#paper-pages mark").first.wait_for()
                check(expanded.locator("#btn-reader-pdf").is_disabled(), "Standalone local reader enabled PDF")
                expanded.close()
                page.screenshot(path=str(backend.RUN / "saved-html-reader.png"))
                generated = backend.ApiTests.vault / "papers" / "generated-local.md"
                generated.write_text("# Generated local study\n\n- Paper ID: " + item["paper_id"] +
                                     "\n\n## Guide\n\nLocal source guide; cites arXiv 2510.16720.", encoding="utf-8")
                backend.app.update_queue_item(backend.ApiTests.vault, item["id"], status="done", note=generated.name)
                page.locator("#btn-reader-close").click()
                page.locator("[data-tab='notes']").click()
                page.evaluate("refreshNotes()")
                page.locator(".note").filter(has_text="Generated local study").click()
                page.locator("#paper-pages mark").first.wait_for()
                check(page.locator("#btn-reader-pdf").is_disabled(), "A cited arXiv number hijacked the local note reader")
                page.locator("#btn-reader-note").click()
                check("Local source guide" in page.locator("#note-md").inner_text(), "Generated local note is unavailable")
                page.locator("#btn-reader-close").click()
                page.locator("[data-tab='queue']").click()
                page.locator(f'[data-read="{item["id"]}"]').click()
                page.locator("#paper-pages mark").first.wait_for()
                if args.sample:
                    response = context.request.post(backend.ApiTests.base + "/api/queue", data={"kind": "html", "value": str(args.sample.resolve())})
                    check(response.ok, "Real snapshot import failed: " + response.text())
                    real = response.json()["item"]
                    page.locator("#btn-reader-close").click()
                    page.evaluate("refreshQueue()")
                    page.locator(f'[data-read="{real["id"]}"]').click()
                    page.locator("#paper-pages math").first.wait_for()
                    metadata = context.request.get(backend.ApiTests.base + "/api/paper?id=" + real["paper_id"] + "&page=1").json()
                    while page.locator("#paper-more").is_visible():
                        before = page.locator("#paper-pages section.paper-page").count()
                        page.locator("#paper-more").click()
                        page.wait_for_function("n => document.querySelectorAll('#paper-pages section.paper-page').length > n", arg=before)
                    check(page.locator("#paper-pages math").count() == metadata["math_count"], "Real snapshot lost formulas while loading chunks")
                    check("Canvas" in page.locator("#paper-source-info").inner_text(), "Real snapshot canvas loss was not disclosed")
                    check(page.evaluate("window.PAPER_SCRIPT_EXECUTED === undefined"), "Real snapshot changed privileged globals")
                    page.screenshot(path=str(backend.RUN / "saved-html-real-snapshot.png"))
                    print("Real snapshot math nodes:", metadata["math_count"])
                check(not external, f"Saved reading attempted external browser requests: {external}")
                check(not errors, f"JavaScript errors: {errors}")
                print(f"PASS: inert HTML, native MathML, raster/SVG, missing-content notices, font/marks/reopen, standalone; artifacts: {backend.RUN}")
            finally:
                browser.close()
    finally:
        backend.ApiTests.tearDownClass()


if __name__ == "__main__":
    main()
