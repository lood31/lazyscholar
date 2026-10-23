"""Probe one public non-arXiv article using Chromium's actual MHTML snapshot.

This is a research/acceptance probe, NOT a production importer. Canvas pixels
are checked separately from DOM element counts. Artifacts include
an ordinary DOM HTML snapshot (not a complete browser save), a self-contained
browser snapshot, and an offline comparison. No paper scripts enter the app.
"""
from __future__ import annotations

import difflib
import json
import re
import sys
import uuid
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
URL = "https://distill.pub/2021/gnn-intro/"

STATS = """() => {
  const article = document.querySelector('d-article, dt-article, article, main') || document.body;
  const images = [...article.querySelectorAll('img')];
  const canvasPixels = [...article.querySelectorAll('canvas')].map(canvas => {
    try {
      const ctx = canvas.getContext('2d');
      if (!ctx) return {width:canvas.width,height:canvas.height,readable:false};
      const data = ctx.getImageData(0,0,canvas.width,canvas.height).data;
      let painted = 0;
      for (let i=3; i<data.length; i+=4) if (data[i]) painted++;
      return {width:canvas.width,height:canvas.height,painted_pixels:painted,readable:true};
    } catch(e) { return {readable:false,error:e.name}; }
  });
  return {
    title: document.title,
    text: article.innerText.replace(/\\s+/g,' ').trim(),
    mathml: article.querySelectorAll('math').length,
    math_components: article.querySelectorAll('d-math,dt-math').length,
    svg: article.querySelectorAll('svg').length,
    canvas: article.querySelectorAll('canvas').length,
    canvas_pixels: canvasPixels,
    images: images.length,
    loaded_images: images.filter(img => img.complete && img.naturalWidth > 0).length,
    article_tag: article.tagName
  };
}"""


def main():
    run = ROOT / "qa" / "saved-html" / uuid.uuid4().hex[:10]
    run.mkdir(parents=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=pw.chromium.executable_path)
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            response = page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            if response is None or response.status != 200 or not page.url.startswith(URL):
                raise RuntimeError(f"Unexpected article response: {page.url}")
            page.locator("d-article, dt-article, article").first.wait_for(timeout=30000)
            for image in page.locator("d-article img, dt-article img, article img").all():
                if image.is_visible():
                    image.scroll_into_view_if_needed()
            page.evaluate("window.scrollTo(0,0)")
            page.wait_for_function("document.readyState === 'complete'", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError as exc:
                print("Network did not become idle:", type(exc).__name__)
            live = page.evaluate(STATS)
            if len(live["text"]) < 5000:
                raise RuntimeError("Public sample does not contain a full article")
            html = page.content()
            (run / "live-article.txt").write_text(live["text"], encoding="utf-8")
            (run / "dom-snapshot.html").write_text(html, encoding="utf-8")
            page.screenshot(path=str(run / "live.png"))
            session = context.new_cdp_session(page)
            snapshot = session.send("Page.captureSnapshot", {"format": "mhtml"})["data"]
            archive = run / "browser-snapshot.mhtml"
            archive.write_bytes(snapshot.encode("utf-8"))
            context.close()

            offline_context = browser.new_context(viewport={"width": 1280, "height": 900}, offline=True)
            offline_page = offline_context.new_page()
            offline_page.goto(archive.resolve().as_uri(), wait_until="load", timeout=30000)
            offline = offline_page.evaluate(STATS)
            (run / "offline-article.txt").write_text(offline["text"], encoding="utf-8")
            (run / "offline-dom.html").write_text(offline_page.content(), encoding="utf-8")
            offline_page.screenshot(path=str(run / "offline.png"))
            offline_context.close()
        finally:
            browser.close()

    sys.path.insert(0, str(ROOT))
    import paper_html

    soup = BeautifulSoup(html, "html.parser")
    article = soup.select_one("d-article, dt-article, article")
    if article is None:
        raise RuntimeError("Sample lost its article element")
    script_count = len(article.find_all("script"))
    # Match the current reader's deliberate safety boundary; do not run scripts.
    for unsafe in list(article.find_all(["script", "style", "noscript", "iframe", "object", "embed", "form", "svg"])):
        unsafe.decompose()
    cleaned = paper_html.bleach.clean(str(article), tags=paper_html.HTML_TAGS | paper_html.MATH_TAGS,
                                      attributes=paper_html._attributes, protocols=["http", "https"], strip=True)
    (run / "inert-content.html").write_text(cleaned, encoding="utf-8")
    inert = BeautifulSoup(cleaned, "html.parser")
    differences = []
    matcher = difflib.SequenceMatcher(None, live["text"], offline["text"], autojunk=False)
    for kind, a0, a1, b0, b1 in matcher.get_opcodes():
        if kind != "equal":
            differences.append({"change": kind, "live": live["text"][a0:a1][:160],
                                "offline": offline["text"][b0:b1][:160]})
    report = {
        "url": URL,
        "method": "Chromium Page.captureSnapshot (MHTML), reopened in a new offline context",
        "ordinary_html_is_dom_only": True,
        "live": {k: v for k, v in live.items() if k != "text"},
        "offline": {k: v for k, v in offline.items() if k != "text"},
        "live_text_chars": len(live["text"]),
        "offline_text_chars": len(offline["text"]),
        "text_matches": live["text"] == offline["text"],
        "text_differences": differences[:20],
        "canvas_pixels_match": live["canvas_pixels"] == offline["canvas_pixels"],
        "current_inert_policy": {
            "mathml": len(inert.find_all("math")),
            "svg": len(inert.find_all("svg")),
            "images": len(inert.find_all("img")),
            "images_with_source": len(inert.select("img[src]")),
            "scripts_removed": script_count,
            "active_content_remaining": bool(inert.find(["script", "iframe", "object", "embed"])),
            "event_attributes_remaining": bool(re.search(r"\son[a-z]+\s*=", cleaned, re.IGNORECASE)),
        },
        "production_importer_implemented": False,
    }
    (run / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))
    print("Artifacts:", run)


if __name__ == "__main__":
    main()
