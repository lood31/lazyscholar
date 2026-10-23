"""Import inert local HTML/MHTML. Never fetch resources or execute paper code."""
from __future__ import annotations

import base64
import hashlib
import io
import re
import urllib.parse
from collections.abc import Callable
from email import policy
from email.parser import BytesParser
from pathlib import Path
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from PIL import Image

import paper_html

MAX_ARCHIVE = 32 * 1024 * 1024
MAX_TOTAL_IMAGES = 96 * 1024 * 1024
MAX_IMAGES = 256
SVG_TAGS = {"svg", "g", "path", "line", "polyline", "polygon", "rect", "circle", "ellipse",
            "text", "tspan", "textPath", "defs", "marker", "clipPath", "mask", "pattern",
            "linearGradient", "radialGradient", "stop", "use", "title", "desc"}
SVG_ATTRS = {"id", "viewBox", "preserveAspectRatio", "x", "y", "x1", "y1", "x2", "y2", "dx", "dy",
             "width", "height", "d", "points", "cx", "cy", "r", "rx", "ry", "transform",
             "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-opacity",
             "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "stroke-dashoffset",
             "opacity", "font-family", "font-size", "font-weight", "font-style", "text-anchor",
             "dominant-baseline", "alignment-baseline", "clip-path", "clip-rule", "mask",
             "offset", "stop-color", "stop-opacity", "gradientUnits", "gradientTransform",
             "spreadMethod", "patternUnits", "patternContentUnits", "patternTransform",
             "markerWidth", "markerHeight", "refX", "refY", "orient", "markerUnits",
             "marker-start", "marker-mid", "marker-end"}
SVG_STYLE = {"fill", "stroke", "stroke-width", "opacity", "fill-opacity", "stroke-opacity",
             "font-family", "font-size", "font-weight", "font-style", "text-anchor",
             "dominant-baseline", "stroke-dasharray", "stroke-linecap", "stroke-linejoin"}


def _local_reference(value: str) -> bool:
    return bool(re.fullmatch(r"url\(\s*#[A-Za-z0-9_.:-]+\s*\)", value))


def safe_svg(data: bytes) -> bytes:
    """A standalone image with an explicit element/attribute whitelist, no CSS URLs."""
    if len(data) > paper_html.MAX_IMAGE or b"<!DOCTYPE" in data.upper():
        raise ValueError("SVG 过大或包含不允许的 DTD")
    try:
        root = SafeET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException as exc:
        raise ValueError("SVG 包含不允许的 XML 结构") from exc

    def local(name):
        return name.rsplit("}", 1)[-1]
    tag_case = {tag.lower(): tag for tag in SVG_TAGS}
    attr_case = {attr.lower(): attr for attr in SVG_ATTRS}
    if local(root.tag).lower() != "svg" or len(list(root.iter())) > 20000:
        raise ValueError("SVG 结构无效或过大")
    for parent in list(root.iter()):
        for child in list(parent):
            if local(child.tag).lower() not in tag_case:
                parent.remove(child)
        for name, value in list(parent.attrib.items()):
            attribute = attr_case.get(local(name).lower(), local(name).lower())
            del parent.attrib[name]
            if attribute == "href":
                if re.fullmatch(r"#[A-Za-z0-9_.:-]+", value):
                    parent.set("href", value)
            elif attribute == "style":
                declarations = []
                for part in value.split(";"):
                    prop, _, val = part.partition(":")
                    prop, val = prop.strip().lower(), val.strip()
                    if (prop in SVG_STYLE and len(val) < 300
                            and not re.search(r"url|expression|@|\\|[<>]", val, re.IGNORECASE)):
                        declarations.append(prop + ":" + val)
                if declarations:
                    parent.set("style", ";".join(declarations))
            elif (attribute in SVG_ATTRS and len(value) <= 200000
                  and (not re.search(r"url|expression|javascript|@|\\", value, re.IGNORECASE)
                       or _local_reference(value))):
                parent.set(attribute, value)
    for node in root.iter():
        node.tag = "{http://www.w3.org/2000/svg}" + tag_case[local(node.tag).lower()]
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _image(data: bytes, svg=False) -> tuple[bytes, str]:
    if not data or len(data) > paper_html.MAX_IMAGE:
        raise ValueError("插图为空或过大")
    if svg:
        return safe_svg(data), "svg"
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 25000000:
            raise ValueError("插图像素过多")
        output = io.BytesIO()
        image.convert("RGBA").save(output, format="PNG")
        result = output.getvalue()
    if len(result) > paper_html.MAX_IMAGE:
        raise ValueError("转换后的插图过大")
    return result, "png"


def _archive(data: bytes) -> tuple[bytes, str, dict]:
    message = BytesParser(policy=policy.default).parsebytes(data)
    html = None
    source_url = ""
    resources = {}
    locations = []
    total = 0
    for count, part in enumerate(message.walk()):
        if count > 1000:
            raise ValueError("网页快照资源过多")
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True) or b""
        if not isinstance(payload, bytes):
            raise TypeError("网页快照资源解码结果无效")
        total += len(payload)
        if total > MAX_TOTAL_IMAGES:
            raise ValueError("网页快照解码后过大")
        location = str(part.get("Content-Location", ""))
        if html is None and part.get_content_type() == "text/html":
            charset = part.get_content_charset() or "utf-8"
            try:
                html = payload.decode(charset, errors="replace").encode("utf-8")
            except LookupError as exc:
                raise ValueError("网页快照声明了不支持的字符编码") from exc
            source_url = location
        if part.get_content_maintype() == "image":
            resource = (payload, part.get_content_type() == "image/svg+xml")
            if location:
                locations.append((location, resource))
            content_id = str(part.get("Content-ID", "")).strip("<>")
            if content_id:
                resources["cid:" + content_id] = resource
    if html is None:
        raise ValueError("MHTML 快照中没有 HTML 正文")
    for location, resource in locations:
        key = urllib.parse.urldefrag(urllib.parse.urljoin(source_url, location))[0]
        resources[key] = resource
    return html, source_url, resources


def import_file(source: Path, storage: Path, *, check_id: Callable[[str], None] | None = None) -> dict:
    if not source.is_file() or source.suffix.lower() not in (".html", ".htm", ".mhtml", ".mht"):
        raise ValueError("请选择 HTML 或 MHTML 网页文件")
    if source.stat().st_size > MAX_ARCHIVE:
        raise ValueError("网页文件超过 32 MB")
    data = source.read_bytes()
    resources = {}
    source_url = ""
    is_archive = source.suffix.lower() in (".mhtml", ".mht")
    if is_archive:
        data, source_url, resources = _archive(data)
    if len(data) > paper_html.MAX_HTML:
        raise ValueError("HTML 正文超过 5 MB")
    soup = BeautifulSoup(data, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else source.stem
    article = (soup.find("article") or soup.select_one("d-article, dt-article")
               or soup.find("main") or soup.select_one('[role="main"]') or soup.body)
    if article is None or len(article.get_text(" ", strip=True)) < 200:
        raise ValueError("未找到足够的论文正文；不把空网页或摘要链接当作全文")
    warnings = ["静态网页导入：不运行原网页脚本，不保留交互；请对照原网页确认正文完整。"]
    if article.name == "body":
        warnings.append("未找到明确的正文区域，已导入页面主体，可能包含导航或其他非正文内容。")
    canvases = article.find_all("canvas")
    for canvas in canvases:
        placeholder = soup.new_tag("p")
        placeholder.string = "[Canvas 图形未随网页保存；请对照原网页，未尝试猜测或补画。]"
        canvas.replace_with(placeholder)
    if canvases:
        warnings.append(f"检测到 {len(canvases)} 个 Canvas：保存文件不包含可恢复的画布像素，已明确标记缺失。")
    for bad in list(article.find_all(["script", "style", "noscript", "iframe", "object", "embed", "form",
                                      "input", "button", "select", "textarea", "link", "meta", "base"])):
        bad.decompose()
    # Browser MHTML can serialize citation popovers as declarative shadow DOM.
    # Keep the visible reference number, not the hidden bibliography at every mention.
    for citation in list(article.find_all(["d-cite", "dt-cite"])):
        label = citation.select_one(".citation-number")
        if label is not None:
            visible = " ".join(str(node).strip() for node in label.find_all(string=True) if str(node).strip())
            citation.replace_with(soup.new_string(visible))
    # Keep native accessibility MathML, not both MathML and KaTeX's CSS-only duplicate.
    for presentation in list(article.select(".katex-html, .MathJax_Preview")):
        presentation.decompose()
    for formula in list(article.select(".katex, .MathJax, mjx-container")):
        native = formula.find("math")
        if native is not None:
            formula.replace_with(native.extract())
    assets = {}
    total = 0
    missing = 0
    svgs = [svg for svg in article.find_all("svg") if not svg.find_parent("svg")]

    def store_image(content: bytes, svg: bool) -> str:
        nonlocal total
        payload, suffix = _image(content, svg)
        key = hashlib.sha256(payload).hexdigest()[:24]
        if key not in assets:
            total += len(payload)
            if total > MAX_TOTAL_IMAGES or len(assets) >= MAX_IMAGES:
                raise ValueError("网页插图总量过大")
            assets[key] = (payload, suffix)
        return key

    for svg in svgs:
        try:
            key = store_image(str(svg).encode("utf-8"), True)
            image = soup.new_tag("img", src="/saved-image/" + key, alt="静态 SVG 图形（交互及部分样式已移除）")
            svg.replace_with(image)
        except (ValueError, ET.ParseError):
            svg.replace_with(soup.new_string("[SVG 无法安全导入，请对照原网页]"))
            missing += 1
    if svgs:
        warnings.append(f"{len(svgs)} 个 SVG 已转为独立静态图像；移除脚本、外部引用和不支持的样式，外观可能简化。")
    for image in article.find_all("img"):
        value = str(image.get("src", ""))
        if value.startswith("/saved-image/") and value.removeprefix("/saved-image/") in assets:
            continue
        try:
            if value.startswith("data:"):
                header, content = value.split(",", 1)
                payload = (base64.b64decode(content, validate=True) if ";base64" in header
                           else urllib.parse.unquote_to_bytes(content))
                is_svg = header.startswith("data:image/svg+xml")
            elif is_archive:
                key = urllib.parse.urldefrag(urllib.parse.urljoin(source_url, value))[0]
                payload, is_svg = resources[key]
            else:
                parsed = urllib.parse.urlsplit(value)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    raise ValueError("未联网补下载插图")
                root = source.parent.resolve()
                path = (root / urllib.parse.unquote(parsed.path)).resolve()
                if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > paper_html.MAX_IMAGE:
                    raise ValueError("插图不在网页目录内或过大")
                payload = path.read_bytes()
                is_svg = path.suffix.lower() == ".svg"
            key = store_image(payload, is_svg)
            image["src"] = "/saved-image/" + key
            image.attrs.pop("srcset", None)
        except (ValueError, KeyError, OSError, ET.ParseError):
            label = image.get("alt", "插图")
            image.replace_with(soup.new_string(f"[{label}未能导入，请对照原网页]"))
            missing += 1
    if missing:
        warnings.append(f"{missing} 张插图缺失或未通过安全校验；未自动联网下载，也未静默忽略。")
    article.name = "article"
    article["class"] = "ltx_document"
    wrapper = BeautifulSoup("<html><head><meta charset='utf-8'><title></title></head><body></body></html>", "html.parser")
    title_tag, body_tag = wrapper.find("title"), wrapper.find("body")
    if title_tag is None or body_tag is None:
        raise ValueError("无法创建本地正文容器")
    title_tag.string = title[:500]
    body_tag.append(article)
    prepared = str(wrapper).encode("utf-8")
    parsed_url = urllib.parse.urlsplit(source_url)
    if parsed_url.scheme not in ("http", "https") or parsed_url.username or parsed_url.password:
        source_url = ""
    identity = prepared + b"\0" + source_url.encode("utf-8")
    paper_id = "local-" + hashlib.sha256(identity).hexdigest()[:24]
    if check_id is not None:
        check_id(paper_id)
    document = paper_html.prepare_document(prepared, paper_id, source_url, local=True)
    document["warnings"] = warnings
    document["import_name"] = source.name
    root = storage / paper_id
    for key, (payload, suffix) in assets.items():
        paper_html._write(root / "images" / f"{key}.{suffix}", payload)
    paper_html._write(root / "article.html", prepared)
    paper_html._json(root / "article.json", document)
    return document
