"""Local, deterministic PDF reflow. Prose is text; complex regions are crops.

No model rewriting or OCR. pdfplumber/PDFium have permissive licenses; do not
introduce a copyleft PDF engine into the MIT application build.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import threading
import uuid
from pathlib import Path

import pdfplumber

VERSION = 4
_HASHES: dict[tuple[str, int, int, int], str] = {}
_LOCK = threading.RLock()  # PDFium rendering is not thread-safe.
CAPTION = re.compile(r"^(?:fig(?:ure)?\.?|table|表|图)\s*\d", re.I)
MATH_FONT = re.compile(r"CMMI|CMSY|CMEX|Math|Symbol|STIX", re.I)


def _write_json(path: Path, data) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def cache_for(pdf: Path, root: Path) -> Path:
    with _LOCK:
        stat = pdf.stat()
        key = (str(pdf.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        digest = _HASHES.get(key)
        if digest is None:
            try:
                with pdf.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
            except OSError as e:
                raise ValueError("无法读取原 PDF，原文件未修改") from e
            if len(_HASHES) >= 128:
                _HASHES.clear()
            _HASHES[key] = digest
        return root / f"v{VERSION}-{digest}"


def _box(obj) -> tuple[float, float, float, float]:
    return (float(obj["x0"]), float(obj["top"]), float(obj["x1"]), float(obj["bottom"]))


def _union(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _touch(a, b, pad=4):
    return a[0] <= b[2] + pad and b[0] <= a[2] + pad and a[1] <= b[3] + pad and b[1] <= a[3] + pad


def _inside(word, box):
    x, y = (word["x0"] + word["x1"]) / 2, (word["top"] + word["bottom"]) / 2
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def _lines(words):
    """Keep a large inter-column gap instead of joining both columns into a line."""
    rows = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        row = next((r for r in reversed(rows[-5:]) if abs(r[0]["top"] - word["top"]) < 3), None)
        if row is None:
            rows.append([word])
        else:
            row.append(word)
    result = []
    for row in rows:
        segments = [[]]
        for word in sorted(row, key=lambda w: w["x0"]):
            if segments[-1] and word["x0"] - segments[-1][-1]["x1"] > 32:
                segments.append([])
            segments[-1].append(word)
        for segment in segments:
            result.append({"text": " ".join(w["text"] for w in segment),
                           "bbox": _union([_box(w) for w in segment]), "words": segment})
    return sorted(result, key=lambda line: (line["bbox"][1], line["bbox"][0]))


def _regions(page, lines, body_size, rotated):
    regions = [(tuple(table.bbox), "table") for table in page.find_tables()]
    rotated_groups = []
    for word in rotated:
        box = _box(word)
        touching = [b for b in rotated_groups if _touch(b, box, 6)]
        rotated_groups = [b for b in rotated_groups if b not in touching]
        rotated_groups.append(_union(touching + [box]))
    regions.extend((box, "rotated") for box in rotated_groups)
    regions.extend((_box(image), "figure") for image in page.images)
    # Connected vector drawings cover charts that are not raster images.
    drawings = [_box(o) for o in page.curves + page.rects + page.lines]
    if len(drawings) > 1000:
        drawings = [_union(drawings)]
    groups = []
    for box in drawings:
        touching = [g for g in groups if _touch(g, box, 7)]
        groups = [g for g in groups if g not in touching]
        groups.append(_union(touching + [box]))
    for box in groups:
        if box[2] - box[0] >= 24 and box[3] - box[1] >= 18:
            regions.append((box, "figure"))
    # Borderless tables: crop only aligned rows next to an explicit table caption.
    for index, line in enumerate(lines):
        if not re.match(r"^(?:table|表)\s*\d", line["text"], re.I):
            continue
        rows = []
        bottom = line["bbox"][3]
        for candidate in lines[index + 1:]:
            box = candidate["bbox"]
            if box[1] - bottom > 35 or box[1] - line["bbox"][3] > 220:
                break
            if abs(box[0] - line["bbox"][0]) > page.width * 0.55:
                continue
            words = candidate["words"]
            gaps = [b["x0"] - a["x1"] for a, b in zip(words, words[1:], strict=False)]
            tabular = any(g > 12 for g in gaps) or (len(words) <= 3 and len(candidate["text"]) < 35)
            if rows and not tabular:
                break
            if tabular:
                rows.append(box)
                bottom = max(bottom, box[3])
        if len(rows) >= 3:
            regions.append((_union(rows), "table"))
    # Display math, not ordinary prose containing an inline symbol.
    for line in lines:
        words = line["words"]
        count = sum(len(w["text"]) for w in words) or 1
        math_count = sum(len(w["text"]) for w in words if MATH_FONT.search(w.get("fontname", "")))
        if math_count / count > 0.3 and len(line["text"]) < 120:
            regions.append((line["bbox"], "equation"))
    # Coalesce overlapping detections so text/figures are never emitted twice.
    merged = []
    for box, kind in regions:
        hit = [(b, k) for b, k in merged if _touch(b, box, 2)]
        merged = [(b, k) for b, k in merged if (b, k) not in hit]
        if hit:
            box = _union([box] + [b for b, _ in hit])
            if any(k == "table" for _, k in hit):
                kind = "table"
        x0, top, x1, bottom = box
        box = (max(0, x0 - 2), max(0, top - 2), min(page.width, x1 + 2), min(page.height, bottom + 2))
        if box[2] > box[0] and box[3] > box[1]:
            merged.append((box, kind))
    return merged


def _reading_order(blocks, width):
    middle = width / 2
    left = [b for b in blocks if b["bbox"][2] < middle + 12 and b["bbox"][0] < middle - 40]
    right = [b for b in blocks if b["bbox"][0] > middle - 12 and b["bbox"][2] > middle + 40]
    if len(left) < 4 or len(right) < 4:
        return sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0]))
    full = sorted([b for b in blocks if b["bbox"][0] < middle - 12 and b["bbox"][2] > middle + 12], key=lambda b: b["bbox"][1])
    remaining = [b for b in blocks if b not in full]
    output = []
    def columns(items):
        return sorted(items, key=lambda b: (b["bbox"][0] >= middle - 12, b["bbox"][1], b["bbox"][0]))
    for span in full:
        before = [b for b in remaining if b["bbox"][3] <= span["bbox"][1] + 2]
        output.extend(columns(before))
        remaining = [b for b in remaining if b not in before]
        output.append(span)
    output.extend(columns(remaining))
    return output


def _extract_page(pdf: Path, root: Path, number: int) -> dict:
    if number < 1:
        raise ValueError("页码必须从 1 开始")
    with _LOCK:
        cache = cache_for(pdf, root)
        target = cache / f"page-{number}.json"
        if target.exists():
            try:
                return json.loads(target.read_text(encoding="utf-8"))
            except (ValueError, OSError) as e:
                raise ValueError("排版缓存损坏，原 PDF 未修改") from e
        cache.mkdir(parents=True, exist_ok=True)
        with pdfplumber.open(pdf) as document:
            if number > len(document.pages):
                raise ValueError("页码超出论文范围")
            page = document.pages[number - 1]
            words = page.extract_words(extra_attrs=["fontname", "size"], x_tolerance_ratio=0.15)
            rotated = [w for w in words if not w.get("upright", True)]
            # Remove only headers repeated on a same-parity neighbouring page.
            # Never assume that all text near a page edge is disposable.
            neighbour = number + 1 if number + 1 < len(document.pages) else number - 3
            if number > 1 and 0 <= neighbour < len(document.pages):
                header = " ".join(w["text"] for w in words if w["top"] < 40)
                other = document.pages[neighbour].extract_words(x_tolerance_ratio=0.15)
                other_header = " ".join(w["text"] for w in other if w["top"] < 40)
                def normal(s):
                    return re.sub(r"[\d\s]+", "", s).casefold()
                if len(normal(header)) > 12 and normal(header) == normal(other_header):
                    words = [w for w in words if w["top"] >= 40]
            words = [w for w in words if w.get("upright", True) and not
                     (w["top"] > page.height - 35 and re.fullmatch(r"\d+", w["text"]))]
            sizes = [w["size"] for w in words if len(w["text"]) > 2]
            body = statistics.median(sizes) if sizes else 11
            lines = _lines(words)
            regions = _regions(page, lines, body, rotated)
            text_lines = _lines([w for w in words if not any(_inside(w, box) for box, _ in regions)])
            blocks = []
            for line in text_lines:
                font = statistics.median(w["size"] for w in line["words"])
                text = line["text"].replace("\u00ad", "")
                bold = sum(len(w["text"]) for w in line["words"] if "bold" in w.get("fontname", "").lower()) > len(text) * 0.65
                kind = ("caption" if CAPTION.match(text) else "heading" if
                        font >= body * 1.18 or (bold and len(text) < 100 and not text.endswith("."))
                        or (len(text) < 80 and re.match(r"^\d+(?:\.\d+)*\s+\D", text)) else "text")
                blocks.append({"kind": kind, "text": text, "bbox": line["bbox"], "size": font,
                               "soft_end": line["text"].endswith("\u00ad")})
            bitmap = page.to_image(resolution=180).original if regions or not words else None
            if not words:
                regions = [((0, 0, page.width, page.height), "scanned")]
            for index, (box, kind) in enumerate(regions):
                if bitmap is None:
                    continue
                sx, sy = bitmap.width / page.width, bitmap.height / page.height
                crop = bitmap.crop((int(box[0] * sx), int(box[1] * sy), int(box[2] * sx), int(box[3] * sy)))
                name = f"page-{number}-asset-{index}.png"
                crop.save(cache / name, format="PNG")
                blocks.append({"kind": kind, "image": name, "bbox": box})
            ordered = _reading_order(blocks, page.width)
            paragraphs = []
            for block in ordered:
                previous = paragraphs[-1] if paragraphs else None
                if (previous and block["kind"] == previous["kind"] == "heading"
                        and abs(block["size"] - previous["size"]) < 1
                        and 0 <= block["bbox"][1] - previous["bbox"][3] < body):
                    previous["text"] += " " + block["text"]
                    previous["bbox"] = _union([previous["bbox"], block["bbox"]])
                elif (previous and block["kind"] == previous["kind"] == "text"
                        and abs(block["bbox"][0] - previous["bbox"][0]) < body * 1.4
                        and 0 <= block["bbox"][1] - previous["bbox"][3] < body):
                    old = previous["text"]
                    separator = "" if old.endswith("-") or previous.get("soft_end") else " "
                    previous["text"] = old + separator + block["text"]
                    previous["soft_end"] = block.get("soft_end", False)
                    previous["bbox"] = _union([previous["bbox"], block["bbox"]])
                else:
                    paragraphs.append(block)
            for index, block in enumerate(paragraphs):
                block["id"] = f"p{number}-b{index}"
                block["bbox"] = list(block["bbox"])
            result = {"page": number, "pages": len(document.pages), "source": cache.name,
                      "blocks": paragraphs, "scanned": not bool(words)}
            _write_json(target, result)
            return result


def extract_page(pdf: Path, root: Path, number: int) -> dict:
    try:
        return _extract_page(pdf, root, number)
    except (ValueError, OSError):
        raise
    except Exception as e:
        raise ValueError(f"无法本地提取这份 PDF，原文件未修改：{e}") from e


def image_path(pdf: Path, root: Path, name: str) -> Path:
    if not re.fullmatch(r"page-\d+-asset-\d+\.png", name):
        raise ValueError("图片名称无效")
    path = cache_for(pdf, root) / name
    if not path.is_file():
        raise ValueError("图片尚未提取，请先加载对应原文页")
    return path


def load_highlights(pdf: Path, root: Path) -> dict:
    with _LOCK:
        cache = cache_for(pdf, root)
        path = cache / "highlights.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"highlights": []}
        except (ValueError, OSError) as e:
            raise ValueError("原文划线文件损坏，已保留原文件") from e
        if not isinstance(data, dict) or not isinstance(data.get("highlights"), list):
            raise ValueError("原文划线文件格式无效")
        return {"source": cache.name, "highlights": data["highlights"]}


def save_highlights(pdf: Path, root: Path, data: dict) -> dict:
    with _LOCK:
        cache = cache_for(pdf, root)
        if data.get("source") != cache.name:
            raise ValueError("论文文件已更新，请重新打开原文后再划线")
        rows = data.get("highlights")
        if not isinstance(rows, list) or len(rows) > 500:
            raise ValueError("划线数量必须在 0–500 之间")
        for row in rows:
            if not isinstance(row, dict) or not re.fullmatch(r"p\d+-b\d+", str(row.get("block", ""))):
                raise ValueError("划线段落无效")
            number = int(row["block"].split("-")[0][1:])
            page = extract_page(pdf, root, number)
            block = next((b for b in page["blocks"] if b["id"] == row["block"]), {})
            text = block.get("text", "")
            start, end = row.get("start"), row.get("end")
            if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(text.encode("utf-16-le")) // 2:
                raise ValueError("划线范围无效")
            quote = text.encode("utf-16-le")[start * 2:end * 2].decode("utf-16-le")
            if row.get("quote") != quote:
                raise ValueError("划线文字与原文不一致")
        cache.mkdir(parents=True, exist_ok=True)
        _write_json(cache / "highlights.json", {"highlights": rows})
        return {"source": cache.name, "highlights": rows}
