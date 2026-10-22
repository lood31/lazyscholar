"""Offline structured article for API, browser and native-input regressions."""
from pathlib import Path

from PIL import Image

import paper_html


def html_fixture() -> bytes:
    return ('''<!doctype html><html><head><title>Structured reader fixture</title></head>
<article class="ltx_document" id="document"><h1>Left paragraph reader fixture</h1>
<section id="S1"><h2>Mathematics and selection</h2>
<p>Left paragraph contains selectable text with <em>formatted words</em> and faithful inline mathematics.
Let <math id="S2.p1.m1" alttext="(M^{2n},I,g)"><mrow><mo>(</mo><msup><mi>M</mi><mrow><mn>2</mn><mi>n</mi></mrow></msup><mo>,</mo><mi>I</mi><mo>,</mo><mi>g</mi><mo>)</mo></mrow></math>
be the manifold. Its frame is <math id="S2.p1.m2" alttext="\\{e_{i}\\}_{i=1}^{2n}"><msubsup><mrow><mo>{</mo><msub><mi>e</mi><mi>i</mi></msub><mo>}</mo></mrow><mrow><mi>i</mi><mo>=</mo><mn>1</mn></mrow><mrow><mn>2</mn><mi>n</mi></mrow></msubsup></math>.</p>
<table class="ltx_equation"><tr><td><math display="block" id="eq1" alttext="\\frac{a_i}{b^2}"><mfrac><msub><mi>a</mi><mi>i</mi></msub><msup><mi>b</mi><mn>2</mn></msup></mfrac></math></td><td class="ltx_eqn_eqno">(1)</td></tr></table>
<p>A unique legacy selection survives conversion and remains attached to this sentence.</p>
<figure><img src="2510.16720/fixture.png" alt="Local fixture figure" width="400" height="100"><figcaption>Preserved local figure</figcaption></figure>
<p><a href="#S2">Go to the final section</a></p></section>
''' + ''.join(f'<p>Further mathematical paragraph {i}. <math><mi>x</mi><mo>=</mo><mn>{i}</mn></math></p>' for i in range(55)) + '''
<section id="S2"><h2>Final section</h2><p>End of the structured article with cross-part navigation.</p></section></article></html>''').encode('utf-8')


def seed_html(vault: Path, arxiv_id="2510.16720", data: bytes | None = None) -> dict:
    root = vault / "html_reader" / arxiv_id
    document = paper_html.store_document(root, html_fixture() if data is None else data, arxiv_id, f"https://arxiv.org/html/{arxiv_id}")
    for name in document["images"]:
        dest = root / "images" / f"{name}.png"
        dest.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (400, 100), "#c5d8f0").save(dest)
    return document
