"""arXiv HTML reader: bounded downloads, inert markup, native MathML and text anchors.

No scripts/styles from a paper are executed. A missing/failed HTML document is
reported explicitly; it is not passed through the lossy PDF formula heuristic.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import ipaddress
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

import bleach
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag
from defusedxml import ElementTree as SafeET
from PIL import Image

VERSION = 1
MAX_HTML = 5 * 1024 * 1024
MAX_IMAGE = 12 * 1024 * 1024
ID_RE = re.compile(r"\d{4}\.\d{4,5}(?:v\d+)?")
PREFIX = "ls-source-"
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
HTML_TAGS = {"article", "section", "div", "span", "p", "h1", "h2", "h3", "h4", "h5", "h6",
             "header", "footer", "blockquote", "em", "strong", "b", "i", "u", "s", "sup", "sub",
             "a", "br", "hr", "ul", "ol", "li", "dl", "dt", "dd", "table", "thead", "tbody", "tfoot",
             "tr", "td", "th", "caption", "figure", "figcaption", "img", "pre", "code", "small"}
MATH_TAGS = {"math", "semantics", "annotation", "mrow", "mi", "mn", "mo", "mtext", "mspace",
             "mfrac", "msqrt", "mroot", "msub", "msup", "msubsup", "munder", "mover", "munderover",
             "mtable", "mtr", "mtd", "mlabeledtr", "mmultiscripts", "mprescripts", "none", "mstyle",
             "mpadded", "mphantom", "menclose", "maction"}
MATH_ATTRS = {"display", "alttext", "mathvariant", "mathsize", "mathcolor", "stretchy", "symmetric",
              "fence", "separator", "form", "lspace", "rspace", "minsize", "maxsize", "movablelimits",
              "largeop", "accent", "accentunder", "displaystyle", "scriptlevel", "columnalign", "rowalign",
              "columnspacing", "rowspacing", "columnlines", "rowlines", "columnspan", "rowspan", "align",
              "width", "height", "depth", "voffset", "notation", "linethickness", "bevelled", "encoding"}
BLOCK_TAGS = {"div", "section", "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dt", "dd", "tr", "blockquote", "pre"}


class HtmlUnavailable(ValueError):
    pass


def _lock(root: Path):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(root.resolve()), threading.RLock())


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _json(path: Path, data) -> None:
    _write(path, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def validate_proxy(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or not parsed.port
            or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("HTML 代理须为 http(s)://主机:端口；暂不支持代理账号密码")
    return value


def _origin(url: str):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "arxiv.org" or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise ValueError("只允许从 https://arxiv.org 下载论文资源")
    return parsed.scheme, parsed.hostname, parsed.port or 443


class _SameOrigin(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        if _origin(target) != _origin(req.full_url):
            raise ValueError("拒绝跨来源重定向")
        return super().redirect_request(req, fp, code, msg, headers, target)


def _download(url: str, proxy: str, limit: int) -> tuple[bytes, str, str]:
    _origin(url)
    configured = validate_proxy(proxy)
    handler = urllib.request.ProxyHandler({"http": configured, "https": configured}) if configured else urllib.request.ProxyHandler()
    opener = urllib.request.build_opener(handler, _SameOrigin())
    # _origin() above restricts this audited URL to HTTPS on arxiv.org.
    request = urllib.request.Request(url, headers={"User-Agent": "LazyScholar/0.2 (personal research tool)", "Accept": "text/html,image/*;q=0.8"})  # noqa: S310
    with opener.open(request, timeout=25) as response:
        if response.status != 200:
            raise HtmlUnavailable(f"论文资源返回 HTTP {response.status}")
        data = response.read(limit + 1)
        if len(data) > limit:
            raise HtmlUnavailable("论文资源超过安全大小限制")
        if response.headers.get("Content-Encoding", "").lower() == "gzip":
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                data = stream.read(limit + 1)
            if len(data) > limit:
                raise HtmlUnavailable("解压后的论文资源超过安全大小限制")
        return data, response.geturl(), response.headers.get_content_type()


def _attributes(tag, name, value):
    if name == "id":
        return bool(re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value))
    if name == "class":
        return True  # Only ltx_* tokens are retained in the normalization pass.
    if tag in MATH_TAGS and name in MATH_ATTRS:
        return (value == "application/x-tex" if name == "encoding" else len(value) <= 20000)
    if name in ("rowspan", "colspan", "width", "height"):
        return bool(re.fullmatch(r"[1-9]\d{0,3}", value))
    return (tag == "a" and name == "href") or (tag == "img" and name in ("src", "alt"))


def _units(node: Tag, soup: BeautifulSoup):
    for child in list(node.children):
        if isinstance(child, NavigableString):
            if child.strip():
                paragraph = soup.new_tag("p")
                paragraph.string = str(child)
                yield paragraph
        elif isinstance(child, Tag):
            if child.name == "section":
                if child.get("id"):
                    marker = soup.new_tag("span", attrs={"id": str(child["id"]), "class": "ls-anchor"})
                    yield marker
                yield from _units(child, soup)
            else:
                yield child


def prepare_document(data: bytes, arxiv_id: str, source_url: str) -> dict:
    if not ID_RE.fullmatch(arxiv_id) or len(data) > MAX_HTML:
        raise HtmlUnavailable("论文编号无效或 HTML 过大")
    _origin(source_url)
    soup = BeautifulSoup(data.decode("utf-8-sig"), "html.parser")
    article = soup.select_one("article.ltx_document")
    if article is None:
        raise HtmlUnavailable("响应不是 arXiv HTML 正文，可能是错误页或验证页")
    title = soup.title.get_text(" ", strip=True) if soup.title else arxiv_id
    base = soup.find("base", href=True)
    base_url = urllib.parse.urljoin(source_url, str(base["href"])) if base else source_url
    _origin(base_url)
    for bad in list(article.find_all(["script", "style", "noscript", "iframe", "object", "embed", "form", "input", "button", "link", "meta", "base", "svg"])):
        bad.decompose()
    cleaned = bleach.clean(str(article), tags=HTML_TAGS | MATH_TAGS, attributes=_attributes,
                           protocols=["http", "https"], strip=True, strip_comments=True)
    safe = BeautifulSoup(cleaned, "html.parser")
    root = safe.find("article")
    if root is None or len(root.get_text()) < 200:
        raise HtmlUnavailable("HTML 正文为空或不完整")
    if len(root.find_all(True)) > 100000:
        raise HtmlUnavailable("HTML 结构过大")
    images = {}
    for tag in root.find_all(True):
        if tag.get("id"):
            tag["id"] = PREFIX + str(tag["id"])
        classes = tag.get("class")
        tag["class"] = " ".join(c for c in classes if c.startswith("ltx_")) if isinstance(classes, list) else ""
        if tag.name == "a":
            href = str(tag.get("href", ""))
            target = urllib.parse.urljoin(base_url, href)
            parsed = urllib.parse.urlsplit(target)
            original = urllib.parse.urlsplit(source_url)
            if href.startswith("#") or (parsed.scheme, parsed.netloc, parsed.path) == (original.scheme, original.netloc, original.path):
                tag["href"] = "#" + PREFIX + parsed.fragment
            elif parsed.scheme in ("https", "http") and parsed.hostname and not parsed.username and not parsed.password:
                try:
                    private = ipaddress.ip_address(parsed.hostname).is_private
                except ValueError:
                    private = parsed.hostname == "localhost" or parsed.hostname.endswith((".localhost", ".local"))
                if private:
                    tag.attrs.pop("href", None)
                else:
                    tag["href"] = target
                    tag["target"], tag["rel"] = "_blank", "noopener noreferrer"
            else:
                tag.attrs.pop("href", None)
        if tag.name == "img":
            target = urllib.parse.urljoin(base_url, str(tag.get("src", "")))
            try:
                _origin(target)
            except ValueError:
                tag.replace_with(safe.new_string("[外部插图未自动加载，请对照原版]"))
                continue
            key = hashlib.sha256(target.encode()).hexdigest()[:24]
            images[key] = target
            tag["src"] = "/api/paper/html-image?id=" + urllib.parse.quote(arxiv_id) + "&name=" + key
            tag["loading"] = "lazy"
    canonical = str(root)
    math_count = len(root.find_all("math"))
    source = f"html-v{VERSION}-" + hashlib.sha256(canonical.encode()).hexdigest()
    pieces, current, budget, math_budget = [], [], 0, 0
    for unit in _units(root, safe):
        size, count = len(str(unit)), len(unit.find_all("math"))
        if current and (budget + size > 40000 or math_budget + count > 48):
            pieces.append(current)
            current, budget, math_budget = [], 0, 0
        current.append(unit)
        budget += size
        math_budget += count
    if current:
        pieces.append(current)
    chunks, all_runs, anchors, plain = [], [], {}, ""
    for number, units in enumerate(pieces, 1):
        wrapper = safe.new_tag("div", attrs={"class": "source-html"})
        for unit in units:
            wrapper.append(unit.extract())
        runs = []
        for node in list(wrapper.descendants):
            if isinstance(node, Tag) and node.name == "math":
                plain += "\ufffc"
            elif isinstance(node, NavigableString) and node.find_parent("math") is None:
                text = str(node)
                if node.find_parent("pre") is None:
                    text = re.sub(r"\s+", " ", text)
                if not text.strip():
                    plain += text
                    continue
                key = f"p{number}-b{len(runs)}"
                span = safe.new_tag("span", attrs={"data-block": key, "class": "ls-text"})
                span.string = text
                node.replace_with(span)
                run = {"id": key, "text": text, "start": len(plain), "page": number}
                plain += text
                run["end"] = len(plain)
                runs.append(run)
                all_runs.append(run)
        plain += "\n"
        for tag in wrapper.find_all(id=True):
            anchors.setdefault(str(tag["id"]), number)
        for table in list(wrapper.find_all("table")):
            scroll = safe.new_tag("div", attrs={"class": "ls-math-scroll"})
            table.wrap(scroll)
        chunks.append({"html": str(wrapper), "runs": runs})
    return {"format": VERSION, "id": arxiv_id, "source": source, "source_url": source_url,
            "title": title[:500], "math_count": math_count, "chunks": chunks,
            "runs": all_runs, "plain": plain, "anchors": anchors, "images": images}


@lru_cache(maxsize=8)
def _read_index(path: str, stamp: int, size: int) -> dict:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        if (not isinstance(document, dict) or not isinstance(document.get("chunks"), list)
                or not document["chunks"] or not isinstance(document.get("runs"), list)
                or not isinstance(document.get("images"), dict) or not isinstance(document.get("anchors"), dict)
                or not isinstance(document.get("source"), str)
                or not re.fullmatch(r"html-v\d+-[a-f0-9]{64}", document["source"])):
            raise ValueError("缓存结构无效")
        return document
    except (ValueError, OSError) as e:
        raise HtmlUnavailable("HTML 缓存损坏；原数据已保留，可重新获取") from e


def store_document(root: Path, data: bytes, arxiv_id: str, source_url: str) -> dict:
    document = prepare_document(data, arxiv_id, source_url)
    with _lock(root):
        _write(root / "article.html", data)
        _json(root / "article.json", document)
    return document


def get_document(root: Path, arxiv_id: str, proxy="", refresh=False) -> dict:
    if not ID_RE.fullmatch(arxiv_id):
        raise HtmlUnavailable("论文编号无效")
    with _lock(root):
        path = root / "article.json"
        if path.exists() and not refresh:
            stat = path.stat()
            document = _read_index(str(path), stat.st_mtime_ns, stat.st_size)
            if document.get("format") == VERSION and document.get("id") == arxiv_id:
                return document
        try:
            data, final, kind = _download(f"https://arxiv.org/html/{arxiv_id}", proxy, MAX_HTML)
            if kind not in ("text/html", "application/xhtml+xml"):
                raise HtmlUnavailable("HTML 入口没有返回网页正文")
            return store_document(root, data, arxiv_id, final)
        except urllib.error.HTTPError as e:
            hint = "本论文暂无 HTML 版" if e.code in (404, 410) else "访问被拒绝或限流，请稍后重试"
            raise HtmlUnavailable(f"{hint}（HTTP {e.code}）；可查看完整原版 PDF") from e
        except (OSError, ValueError) as e:
            if isinstance(e, HtmlUnavailable):
                raise
            raise HtmlUnavailable(f"无法获取 HTML：{e}；可重试或查看完整原版 PDF") from e


def get_page(root: Path, arxiv_id: str, number: int, proxy="", refresh=False) -> dict:
    document = get_document(root, arxiv_id, proxy, refresh)
    if not 1 <= number <= len(document["chunks"]):
        raise ValueError("原文部分编号超出范围")
    chunk = document["chunks"][number - 1]
    return {"page": number, "pages": len(document["chunks"]), "source": document["source"],
            "source_kind": "html", "source_url": document["source_url"], "math_count": document["math_count"],
            "blocks": [{"kind": "html", **chunk}], "scanned": False}


def get_image(root: Path, arxiv_id: str, name: str, proxy="") -> tuple[bytes, str]:
    if not re.fullmatch(r"[a-f0-9]{24}", name):
        raise ValueError("图片编号无效")
    document = get_document(root, arxiv_id, proxy)
    url = document["images"].get(name)
    if not url:
        raise ValueError("图片不属于这篇论文")
    with _lock(root):
        png, svg = root / "images" / f"{name}.png", root / "images" / f"{name}.svg"
        if png.exists():
            return png.read_bytes(), "image/png"
        if svg.exists():
            return svg.read_bytes(), "image/svg+xml"
        data, _, kind = _download(url, proxy, MAX_IMAGE)
        if kind == "image/svg+xml":
            text = data.decode("utf-8-sig")
            if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
                raise ValueError("SVG 不允许外部实体")
            image = SafeET.fromstring(text)
            if image.tag.split("}")[-1].lower() != "svg":
                raise ValueError("图片不是 SVG 文档")
            for parent in image.iter():
                for child in list(parent):
                    if child.tag.split("}")[-1].lower() in ("script", "foreignobject", "iframe", "object", "embed"):
                        parent.remove(child)
                for key, value in list(parent.attrib.items()):
                    local = key.split("}")[-1].lower()
                    if local.startswith("on") or (local == "href" and not value.startswith("#")):
                        del parent.attrib[key]
            data = ET.tostring(image, encoding="utf-8")
            _write(svg, data)
            return data, "image/svg+xml"
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 25000000:
                raise ValueError("图片像素过大")
            output = io.BytesIO()
            image.convert("RGBA").save(output, "PNG")
        data = output.getvalue()
        _write(png, data)
        return data, "image/png"


def _annotation_path(root: Path, document: dict) -> Path:
    if not re.fullmatch(r"html-v\d+-[a-f0-9]{64}", document["source"]):
        raise ValueError("HTML 来源标识无效")
    return root / (document["source"] + "-highlights.json")


def load_highlights(root: Path, document: dict, legacy: Path | None = None) -> dict:
    with _lock(root):
        path = _annotation_path(root, document)
        if path.exists():
            try:
                result = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as e:
                raise ValueError("HTML 划线文件损坏，已保留原文件") from e
            if (not isinstance(result, dict) or not isinstance(result.get("highlights"), list)
                    or not isinstance(result.get("legacy_unresolved", []), list)):
                raise ValueError("HTML 划线文件格式无效")
            return {"source": document["source"], **result}
        result = {"highlights": [], "legacy_unresolved": []}
        if legacy is not None and legacy.exists():
            try:
                old = json.loads(legacy.read_text(encoding="utf-8"))
            except (ValueError, OSError) as e:
                raise ValueError("旧版划线文件损坏，未覆盖原数据") from e
            if not isinstance(old, dict) or not isinstance(old.get("highlights", []), list):
                raise ValueError("旧版划线文件格式无效，已保留原数据")
            for row in old.get("highlights", []):
                if not isinstance(row, dict) or not isinstance(row.get("quote", ""), str):
                    raise ValueError("旧版划线记录无效，已保留原数据")
                quote = row.get("quote", "")
                start = document["plain"].find(quote) if quote else -1
                if start < 0 or document["plain"].find(quote, start + 1) >= 0:
                    result["legacy_unresolved"].append(row)
                    continue
                end = start + len(quote)
                for run in document["runs"]:
                    a, b = max(start, run["start"]), min(end, run["end"])
                    if b > a:
                        first, last = a - run["start"], b - run["start"]
                        result["highlights"].append({"block": run["id"], "start": len(run["text"][:first].encode("utf-16-le")) // 2,
                                                     "end": len(run["text"][:last].encode("utf-16-le")) // 2,
                                                     "quote": run["text"][first:last]})
            _json(path, result)
        return {"source": document["source"], **result}


def save_highlights(root: Path, document: dict, data: dict, legacy: Path | None = None) -> dict:
    with _lock(root):
        if data.get("source") != document["source"]:
            raise ValueError("原文来源已改变，请重新打开后再划线")
        rows = data.get("highlights")
        if not isinstance(rows, list) or len(rows) > 5000:
            raise ValueError("划线片段数量必须在 0–5000 之间")
        texts = {run["id"]: run["text"] for run in document["runs"]}
        for row in rows:
            if not isinstance(row, dict) or row.get("block") not in texts:
                raise ValueError("划线文字段不存在")
            text = texts[row["block"]].encode("utf-16-le")
            first, last = row.get("start"), row.get("end")
            if not isinstance(first, int) or not isinstance(last, int) or not 0 <= first < last <= len(text) // 2:
                raise ValueError("划线范围无效")
            if text[first * 2:last * 2].decode("utf-16-le") != row.get("quote"):
                raise ValueError("划线与原文不一致")
        previous = load_highlights(root, document, legacy)
        result = {"highlights": rows, "legacy_unresolved": previous.get("legacy_unresolved", [])}
        _json(_annotation_path(root, document), result)
        return {"source": document["source"], **result}
