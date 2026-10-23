"""Reversible paper lifecycle operations. Only rename known, vault-owned paths.

No permanent deletion, external source-file access, or recursive removal is used.
The journal is written before moving anything so interrupted operations can restore.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import paper_html

ROOT = ".paper-trash"
ARXIV = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?")
ENTRY = re.compile(r"[a-f0-9]{32}")
_condition = threading.Condition()
_gates: dict[str, dict] = {}


class Busy(ValueError):
    pass


class Gone(ValueError):
    pass


def _gate(vault: Path) -> dict:
    return _gates.setdefault(str(vault.resolve()), {"readers": 0, "writer": False})


@contextmanager
def reading(vault: Path) -> Iterator[None]:
    """Readers stay concurrent; lifecycle writers never race a cache/mark request."""
    with _condition:
        gate = _gate(vault)
        _condition.wait_for(lambda: not gate["writer"])
        gate["readers"] += 1
    try:
        yield
    finally:
        with _condition:
            gate["readers"] -= 1
            _condition.notify_all()


@contextmanager
def changing(vault: Path) -> Iterator[None]:
    with _condition:
        gate = _gate(vault)
        if gate["writer"] or gate["readers"]:
            raise Busy("原文正在读取或标注正在保存，请稍后再试")
        gate["writer"] = True
    try:
        yield
    finally:
        with _condition:
            gate["writer"] = False
            _condition.notify_all()


def _safe(vault: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if not parts or relative != "/".join(parts) or any(p in (".", "..") for p in parts) or any(c in relative for c in ("\\", ":", "\0")):
        raise ValueError("回收站路径无效")
    root = vault.resolve()
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink() or path.is_junction():
            raise ValueError("不处理符号链接或目录联接，原文件未改动")
    if not path.resolve().is_relative_to(root):
        raise ValueError("文件不在应用保存目录内")
    return path


def _json(path: Path, data) -> None:
    paper_html._write(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def _index(vault: Path) -> dict:
    path = _safe(vault, ROOT + "/index.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("无法读取回收站索引，未改动任何文件") from exc
    if not isinstance(data, dict) or any(not paper_html.valid_document_id(key) or not isinstance(value, str) or not ENTRY.fullmatch(value)
                                         for key, value in data.items()):
        raise ValueError("回收站索引损坏，未改动文件")
    return data


def canonical_id(value: str) -> str:
    if not isinstance(value, str) or not paper_html.valid_document_id(value):
        raise ValueError("论文编号无效")
    match = ARXIV.fullmatch(value)
    return match.group(1) if match else value


def deleted_ids(vault: Path) -> set[str]:
    return set(_index(vault))


def ensure_active(vault: Path, paper_id: str) -> None:
    if not paper_html.valid_document_id(paper_id):
        raise ValueError("论文编号无效")
    if canonical_id(paper_id) in _index(vault):
        raise Gone("论文已移入回收站，请先恢复；未重新下载原文或保存标注")


def note_id(path: Path) -> str | None:
    """Only identity metadata or an app-style filename, never an arbitrary citation."""
    with path.open(encoding="utf-8", errors="replace") as stream:
        text = stream.read(8192)
    match = re.search(r"^- Paper ID: ([^\s]+)$", text, re.MULTILINE)
    if match:
        value = match.group(1)
        return canonical_id(value) if paper_html.valid_document_id(value) else None
    for line in text.splitlines():
        if re.match(r"^\s*(?:-\s*)?arxiv\s*:", line, re.IGNORECASE):
            match = ARXIV.search(line)
            if match:
                return match.group(1)
    match = re.match(r"^(local-[a-f0-9]{24}|\d{4}\.\d{4,5}(?:v\d+)?)-", path.name)
    return canonical_id(match.group(1)) if match else None


def queue_id(item: dict) -> str | None:
    if item.get("kind") == "html":
        value = item.get("paper_id", "")
        return value if isinstance(value, str) and paper_html.LOCAL_ID_RE.fullmatch(value) else None
    source = item.get("label", "") if item.get("kind") == "pdf" else item.get("value", "")
    match = ARXIV.search(source) if isinstance(source, str) else None
    return match.group(1) if match else None


def _allowed(paper_id: str, relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    if len(parts) != 2:
        return False
    kind, name = parts
    if kind == "papers":
        return name.endswith(".md")
    if kind in ("reader", "html_reader"):
        return name == paper_id
    if kind == "papers_pdf":
        return name == paper_id + ".pdf"
    if kind == "highlights":
        return name == paper_id + ".json"
    if kind == "fulltext":
        return re.fullmatch(re.escape(paper_id) + r"-[a-f0-9]{16}\.json", name) is not None
    if kind == "queue_files":
        return re.fullmatch(r"[a-f0-9]{10}\.pdf", name) is not None
    if kind == "processed":
        return re.fullmatch(r"[a-f0-9]{10}-.+\.pdf", name, re.IGNORECASE) is not None
    return False


def _record(vault: Path, entry: str) -> tuple[Path, dict]:
    if not isinstance(entry, str) or not ENTRY.fullmatch(entry):
        raise ValueError("回收站项目编号无效")
    root = _safe(vault, ROOT + "/" + entry)
    try:
        data = json.loads(_safe(vault, ROOT + "/" + entry + "/manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("无法读取回收站记录，未改动任何文件") from exc
    if (not isinstance(data, dict) or data.get("entry") != entry or not isinstance(data.get("paper_id"), str)
            or not paper_html.valid_document_id(data["paper_id"]) or not isinstance(data.get("paths"), list)
            or not all(isinstance(path, str) for path in data["paths"]) or not isinstance(data.get("queue"), list)
            or not isinstance(data.get("title"), str) or not isinstance(data.get("date"), str)
            or data.get("state") not in ("moving", "trashed", "restoring", "restored")
            or len(data["paths"]) != len(set(data["paths"]))):
        raise ValueError("回收站记录格式无效")
    for relative in data["paths"]:
        if not isinstance(relative, str) or not _allowed(data["paper_id"], relative):
            raise ValueError("回收站记录包含非论文路径，未恢复任何文件")
        _safe(vault, relative)
        _safe(vault, ROOT + "/" + entry + "/files/" + relative)
    if any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
           or queue_id(item) != data["paper_id"] for item in data["queue"]):
        raise ValueError("回收站队列记录不属于该论文")
    return root, data


def list_entries(vault: Path) -> list:
    entries = []
    for paper_id, entry in _index(vault).items():
        _root, data = _record(vault, entry)
        entries.append({"id": entry, "paper_id": paper_id, "title": data["title"], "date": data["date"], "state": data["state"]})
    return sorted(entries, key=lambda row: row["date"], reverse=True)


def _move(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise Busy("目标位置已有文件，未覆盖任何文件")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)


def _check_tree(path: Path) -> None:
    if path.is_dir():
        for member in path.rglob("*"):
            if member.is_symlink() or member.is_junction():
                raise ValueError("缓存含有符号链接或目录联接，未移动文件")


def restore(vault: Path, entry: str, items: list, save_queue: Callable[[list], None]) -> dict:
    """Caller holds changing() and its queue/batch locks. Preflight before any moves."""
    root, data = _record(vault, entry)
    index = _index(vault)
    paper_id = data["paper_id"]
    if index.get(paper_id) != entry:
        raise Busy("该项目已恢复或不再是当前回收站项目")
    restored_ids = {row["id"] for row in data["queue"]}
    if any(queue_id(row) == paper_id and row not in data["queue"] or row.get("id") in restored_ids and row not in data["queue"] for row in items):
        raise Busy("队列已有同编号项目，未覆盖；请先处理冲突")
    moves = []
    for relative in data["paths"]:
        original = _safe(vault, relative)
        cached = _safe(vault, ROOT + "/" + entry + "/files/" + relative)
        if original.exists() and cached.exists():
            raise Busy("恢复位置已有同名文件，未覆盖；请先处理冲突")
        if not original.exists() and not cached.exists():
            raise ValueError("回收站记录中的文件缺失，未恢复任何文件")
        if cached.exists():
            _check_tree(cached)
            moves.append((cached, original))
    data["state"] = "restoring"
    _json(root / "manifest.json", data)
    for cached, original in moves:
        _move(cached, original)
    save_queue(items + [row for row in data["queue"] if row not in items])
    data["state"] = "restored"
    _json(root / "manifest.json", data)
    # Clearing the tombstone is the final commit; interrupted restores stay resumable.
    del index[paper_id]
    _json(_safe(vault, ROOT + "/index.json"), index)
    return {"ok": True, "paper_id": paper_id}


def trash(vault: Path, items: list, save_queue: Callable[[list], None], *, name: str = "", queue_item: str = "") -> dict:
    """Move exactly one identified paper and its known app-owned data, never sources."""
    if bool(name) == bool(queue_item):
        raise ValueError("请指定一个论文笔记或队列项目")
    title = name
    if name:
        if Path(name).name != name or not name.endswith(".md"):
            raise ValueError("导读文件名无效")
        note = _safe(vault, "papers/" + name)
        if not note.is_file():
            raise ValueError("导读不存在或已移入回收站")
        paper_id = note_id(note)
    else:
        selected = next((row for row in items if row.get("id") == queue_item), None)
        if selected is None:
            raise ValueError("队列项目不存在")
        paper_id = queue_id(selected)
        title = selected.get("label") or paper_id
    if not paper_id:
        raise ValueError("无法安全确认论文编号，未移动任何文件")
    ensure_active(vault, paper_id)
    related = [row for row in items if queue_id(row) == paper_id]
    if any(row.get("status") == "processing" for row in related):
        raise Busy("论文正在生成导读，请完成后再操作")
    paths = []
    for path in (vault / "papers").glob("*.md"):
        safe = _safe(vault, "papers/" + path.name)
        if note_id(safe) == paper_id:
            paths.append("papers/" + path.name)
    paths += ["papers_pdf/" + paper_id + ".pdf", "reader/" + paper_id, "html_reader/" + paper_id,
              "highlights/" + paper_id + ".json"]
    paths += ["fulltext/" + path.name for path in (vault / "fulltext").glob(paper_id + "-*.json")
              if _allowed(paper_id, "fulltext/" + path.name)]
    for row in related:
        if row.get("kind") != "pdf":
            continue
        identifier = row.get("id", "")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{10}", identifier):
            raise ValueError("队列文件归属不明确，未移动任何文件")
        stored = row.get("stored", "")
        if stored and stored != identifier + ".pdf":
            raise ValueError("队列文件路径异常，未移动任何文件")
        if stored:
            paths.append("queue_files/" + stored)
        paths.append("processed/" + identifier + "-" + Path(row.get("label", "paper.pdf")).name)
    planned = []
    for relative in dict.fromkeys(paths):
        if not _allowed(paper_id, relative):
            raise ValueError("待移动路径不属于该论文，未移动任何文件")
        path = _safe(vault, relative)
        if path.exists():
            _check_tree(path)
            planned.append(relative)
    entry = uuid.uuid4().hex
    root = _safe(vault, ROOT + "/" + entry)
    data = {"entry": entry, "paper_id": paper_id, "title": title, "date": datetime.now(UTC).isoformat(timespec="seconds"),
            "state": "moving", "paths": planned, "queue": related}
    _json(root / "manifest.json", data)
    index = _index(vault)
    index[paper_id] = entry
    _json(_safe(vault, ROOT + "/index.json"), index)
    try:
        for relative in planned:
            _move(_safe(vault, relative), _safe(vault, ROOT + "/" + entry + "/files/" + relative))
        save_queue([row for row in items if row not in related])
        data["state"] = "trashed"
        _json(root / "manifest.json", data)
    except Exception:
        try:
            restore(vault, entry, items, save_queue)
        except Exception as recovery_error:
            # The durable manifest + tombstone retain all paths for an explicit retry.
            raise OSError("移入回收站未完成，自动恢复失败；请从回收站重试恢复，未永久删除文件") from recovery_error
        raise
    return {"ok": True, "id": entry, "paper_id": paper_id, "paths": planned}
