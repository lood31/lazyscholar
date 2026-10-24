"""Native paper recycling plus metadata receipts; private payloads are legacy-only.

No permanent deletion or external original-source targets. Old private records can
only be restored explicitly; new deletions are performed by Windows.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import paper_html

ROOT = ".paper-trash"
ARXIV = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?")
ENTRY = re.compile(r"[a-f0-9]{32}")
_condition = threading.Condition()
_gates: dict[str, dict] = {}
_import_permit: ContextVar[tuple[str, str] | None] = ContextVar("paper_import_permit", default=None)


class Busy(ValueError):
    pass


class Gone(ValueError):
    def __init__(self, message, *, paper_id="", reimport_id=""):
        super().__init__(message)
        self.paper_id = paper_id
        self.reimport_id = reimport_id


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
        # Let short metadata reads settle, but never move files under an active reader.
        if not _condition.wait_for(lambda: not gate["writer"] and not gate["readers"], timeout=.2):
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
    return set(_index(vault)) | {identity for identity, row in _native(vault).items() if row["state"] in _BLOCKED}


def ensure_active(vault: Path, paper_id: str) -> None:
    identity = canonical_id(paper_id)
    permit = _import_permit.get()
    if permit and permit != (str(vault.resolve()), identity):
        raise ValueError("重新导入的文件与所选论文不一致")
    if identity in _index(vault):
        raise Gone("旧版回收记录尚未恢复，请先恢复旧数据；原文件保持不变")
    record = _native(vault).get(identity)
    if record and record["state"] in _BLOCKED and not permit:
        raise Gone("论文已删除，可在 Windows 回收站还原，或明确重新导入。", paper_id=identity, reimport_id=record["id"])


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


# Native recycling retains metadata receipts, never copies of paper contents.
RECEIPTS = "paper_deletions.json"
_BLOCKED = {"recycling", "partial", "recycled", "metadata_pending", "queue_removed"}


def _native(vault: Path) -> dict:
    path = _safe(vault, RECEIPTS)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("无法读取删除记录，未改动文件") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("papers"), dict):
        raise ValueError("删除记录格式无效")
    for identity, row in data["papers"].items():
        if (not paper_html.valid_document_id(identity) or canonical_id(identity) != identity
                or not isinstance(row, dict) or row.get("paper_id") != identity
                or not isinstance(row.get("id"), str) or not ENTRY.fullmatch(row["id"])
                or row.get("state") not in _BLOCKED | {"active"}
                or not isinstance(row.get("paths"), list) or not isinstance(row.get("queue"), list)
                or not isinstance(row.get("fingerprints"), dict) or not isinstance(row.get("title"), str)
                or not isinstance(row.get("epoch", ""), str)
                or row.get("epoch") and not ENTRY.fullmatch(row["epoch"])):
            raise ValueError("删除记录包含无效论文")
        for relative in row["paths"]:
            if not isinstance(relative, str) or not _allowed(identity, relative):
                raise ValueError("删除记录包含非论文路径")
            _safe(vault, relative)
            if not isinstance(row["fingerprints"].get(relative), str) or not re.fullmatch(r"[a-f0-9]{64}", row["fingerprints"][relative]):
                raise ValueError("删除记录的文件指纹无效")
        if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or queue_id(item) != identity for item in row["queue"]):
            raise ValueError("删除记录中的队列不属于该论文")
    return data["papers"]


def _save_native(vault: Path, data: dict) -> None:
    _json(_safe(vault, RECEIPTS), {"version": 1, "papers": data})


def lifecycle_epoch(vault: Path, identity: str) -> str:
    return _native(vault).get(canonical_id(identity), {}).get("epoch", "")


def check_lifecycle(vault: Path, identity: str, epoch: str) -> None:
    ensure_active(vault, identity)
    current = lifecycle_epoch(vault, identity)
    if current and epoch != current:
        raise Gone("阅读状态已经更新，请重新打开论文；旧窗口不能写入新状态")


def _fingerprint(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    members = sorted(path.rglob("*")) if path.is_dir() else [path]
    digest.update(b"directory" if path.is_dir() else b"file")
    for member in members:
        if member.is_symlink() or member.is_junction():
            raise ValueError("路径含符号链接或目录联接")
        digest.update((member.relative_to(path).as_posix() if path.is_dir() else member.name).encode("utf-8"))
        digest.update(b"D" if member.is_dir() else b"F")
        if member.is_file():
            with member.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def _collect(vault: Path, items: list, *, name="", queue_item="") -> dict:
    if bool(name) == bool(queue_item):
        raise ValueError("请指定一篇论文")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError("队列格式异常，未改动文件")
    title = name
    if name:
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(".md"):
            raise ValueError("导读文件名无效")
        note = _safe(vault, "papers/" + name)
        if not note.is_file():
            raise ValueError("导读不存在或已删除")
        identity = note_id(note)
        with note.open(encoding="utf-8", errors="replace") as stream:
            title = stream.readline(1000).lstrip("# ").strip() or note.stem
    else:
        selected = next((row for row in items if row.get("id") == queue_item), None)
        if selected is None:
            raise ValueError("队列项目不存在")
        identity, title = queue_id(selected), selected.get("label", "")
    if not identity:
        raise ValueError("无法安全确认论文编号，未改动文件")
    ensure_active(vault, identity)
    if not isinstance(title, str):
        raise TypeError("论文标题格式异常")
    related = [row for row in items if queue_id(row) == identity]
    for row in related:
        identifier = row.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{10}", identifier):
            raise ValueError("队列编号异常，未改动文件")
        if any(other.get("id") == identifier and queue_id(other) != identity for other in items):
            raise ValueError("队列编号被不同论文共用，无法安全确认文件归属")
    if any(row.get("status") == "processing" for row in related):
        raise Busy("论文正在生成导读，请完成后再操作")
    paths = []
    for path in (vault / "papers").glob("*.md"):
        safe = _safe(vault, "papers/" + path.name)
        if note_id(safe) == identity:
            paths.append("papers/" + path.name)
    paths += ["papers_pdf/" + identity + ".pdf", "reader/" + identity, "html_reader/" + identity,
              "highlights/" + identity + ".json"]
    paths += ["fulltext/" + path.name for path in (vault / "fulltext").glob(identity + "-*.json")
              if _allowed(identity, "fulltext/" + path.name)]
    for row in related:
        if row.get("kind") != "pdf":
            continue
        identifier = row.get("id", "")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{10}", identifier):
            raise ValueError("队列文件归属不明确")
        stored = row.get("stored", "")
        if stored and stored != identifier + ".pdf":
            raise ValueError("队列文件路径异常")
        if stored:
            paths.append("queue_files/" + stored)
        paths.append("processed/" + identifier + "-" + Path(row.get("label", "paper.pdf")).name)
    planned, fingerprints = [], {}
    for relative in dict.fromkeys(paths):
        if not _allowed(identity, relative):
            raise ValueError("路径不属于该论文")
        path = _safe(vault, relative)
        if path.exists():
            _check_tree(path)
            planned.append(relative)
            fingerprints[relative] = _fingerprint(path)
    return {"paper_id": identity, "title": title or identity, "paths": planned, "fingerprints": fingerprints,
            "queue": related, "lifecycle": lifecycle_epoch(vault, identity)}


def _preview_token(data: dict) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def preview(vault: Path, items: list, *, name="", queue_item="", receipt_id="") -> dict:
    if receipt_id:
        row = next((row for row in _native(vault).values() if row["id"] == receipt_id and row["state"] in _BLOCKED), None)
        if row is None:
            raise ValueError("删除操作记录不存在")
        if any(not _safe(vault, rel).exists() and rel not in row["completed"] for rel in row["paths"]):
            raise Busy("上次操作的系统结果尚未确认，请在 Windows 回收站核实或还原后再试")
        queue_only = not row["paths"]
        remaining = [relative for relative in row["paths"] if _safe(vault, relative).exists()]
        data = {"paper_id": row["paper_id"], "title": row["title"], "paths": remaining,
                "fingerprints": {rel: _fingerprint(_safe(vault, rel)) for rel in remaining},
                "queue": [item for item in items if queue_id(item) == row["paper_id"]], "receipt_id": receipt_id,
                "lifecycle": row["epoch"]}
        if any(data["fingerprints"][rel] != row["fingerprints"][rel] for rel in remaining):
            raise Busy("剩余文件内容已经变化，未再次回收；请先核实文件")
    else:
        data = _collect(vault, items, name=name, queue_item=queue_item)
        queue_only = not data["paths"]
    return {**data, "token": _preview_token(data), "count": len(data["paths"]), "queue_only": queue_only}


def trash(vault: Path, items: list, save_queue: Callable[[list], None], *, name="", queue_item="", token="", receipt_id="") -> dict:
    """Confirmed native recycle operation. No private payload directory is created."""
    import windows_recycle
    data = preview(vault, items, name=name, queue_item=queue_item, receipt_id=receipt_id)
    if not token or token != data.pop("token"):
        raise Busy("删除预览已经变化，请重新选择并确认")
    data.pop("count")
    data.pop("queue_only")
    identity = data["paper_id"]
    records = _native(vault)
    previous = records.get(identity)
    row = dict(previous) if receipt_id and previous else {
        **data, "id": uuid.uuid4().hex, "epoch": previous.get("epoch", "") if previous else "",
        "date": datetime.now(UTC).isoformat(timespec="seconds"), "completed": [], "locations": {}}
    row["state"] = "recycling"
    records[identity] = row
    _save_native(vault, records)
    persisted_completed = set(row["completed"])
    paths = [_safe(vault, relative) for relative in data["paths"]]
    try:
        result = windows_recycle.recycle(paths) if paths else windows_recycle.Result()
    except (OSError, ValueError) as exc:
        result = windows_recycle.Result(error=str(exc))
    native_locations = {Path(source): location for source, location in result.completed.items()}
    confirmed = [relative for relative, path in zip(data["paths"], paths, strict=True) if path in native_locations and not path.exists()]
    absent = [relative for relative in row["paths"] if not _safe(vault, relative).exists()]
    row["completed"] = list(dict.fromkeys(row.get("completed", []) + confirmed))
    row["locations"] = {**row.get("locations", {}), **{rel: native_locations[_safe(vault, rel)] for rel in confirmed}}
    changed = bool(absent) or not row["paths"]
    complete = result.succeeded(paths) and all(relative in row["completed"] for relative in row["paths"])

    def persist_result() -> dict | None:
        nonlocal persisted_completed
        try:
            _save_native(vault, records)
        except OSError as exc:
            # The initial journal still blocks this ID. Never hide confirmed OS
            # results or offer blind retry for newly missing, unpersisted items.
            unknown = any(relative not in persisted_completed for relative in absent)
            return {"ok": False, "partial": changed, "receipt_pending": True,
                    "restore_required": unknown, "metadata_pending": not unknown,
                    "paper_id": identity, "id": row["id"], "completed": len(row["completed"]),
                    "remaining": len(row["paths"]) - len(absent),
                    "msg": "系统操作后记录保存失败，请核实 Windows 回收站并修复保存目录后刷新：" + str(exc)[:200]}
        persisted_completed = set(row["completed"])
        return None

    if complete:
        row["state"] = "metadata_pending"
        row["epoch"] = uuid.uuid4().hex
        # Persist confirmed OS results and unfinished queue work together. A crash
        # or queue-write error stays discoverable without another successful write.
        if failure := persist_result():
            return failure
        try:
            save_queue([item for item in items if queue_id(item) != identity])
        except OSError:
            return {"ok": False, "partial": True, "metadata_pending": True, "paper_id": identity, "id": row["id"],
                    "completed": len(row["completed"]), "remaining": 0, "msg": "文件已进入系统回收站，队列状态更新失败，可重试收尾"}
        row["state"] = "recycled" if row["paths"] else "queue_removed"
        if failure := persist_result():
            return failure
        return {"ok": True, "paper_id": identity, "id": row["id"], "count": len(row["completed"]), "queue_only": not row["paths"]}
    if changed:
        row["state"] = "partial"
        row["epoch"] = uuid.uuid4().hex
        row["error"] = result.error or "系统未确认全部项目"
        if failure := persist_result():
            return failure
    else:
        if previous:
            records[identity] = previous
        else:
            del records[identity]
        if failure := persist_result():
            return failure
    return {"ok": False, "partial": changed, "paper_id": identity, "id": row["id"],
            "completed": len(row["completed"]), "remaining": len(row["paths"]) - len(absent),
            "msg": "部分项目已交给系统回收站，请还原或重试剩余项目" if changed else (result.error or "系统回收未完成，文件保持原位")}


def reconcile(vault: Path, items: list, save_queue: Callable[[list], None]) -> list:
    """Observe Windows restores; never move or overwrite restored file contents."""
    records = _native(vault)
    activated = []
    for identity, row in records.items():
        if row["state"] not in _BLOCKED or not row["paths"]:
            continue
        if not all(_safe(vault, rel).exists() for rel in row["paths"]):
            continue
        if any(_fingerprint(_safe(vault, rel)) != row["fingerprints"][rel] for rel in row["paths"]):
            continue
        identifiers = {item["id"] for item in row["queue"]}
        if any((queue_id(item) == identity or item.get("id") in identifiers) and item not in row["queue"] for item in items):
            continue
        restored = items + [item for item in row["queue"] if item not in items]
        save_queue(restored)
        items = restored
        row["state"] = "active"
        row["epoch"] = uuid.uuid4().hex
        _save_native(vault, records)
        activated.append(identity)
    return activated


@contextmanager
def reactivating(vault: Path, identity: str, receipt: str) -> Iterator[None]:
    identity = canonical_id(identity)
    records = _native(vault)
    row = records.get(identity)
    if not row or row["id"] != receipt or row["state"] not in _BLOCKED:
        raise Busy("删除记录已经变化，请重新确认导入")
    if identity in _index(vault):
        raise Busy("请先处理旧版回收记录")
    permit = _import_permit.set((str(vault.resolve()), identity))
    try:
        yield
        row["state"] = "active"
        row["epoch"] = uuid.uuid4().hex
        _save_native(vault, records)
    finally:
        _import_permit.reset(permit)


def activate_legacy_restored(vault: Path, identity: str) -> None:
    identity = canonical_id(identity)
    records = _native(vault)
    records[identity] = {"id": uuid.uuid4().hex, "paper_id": identity, "title": identity, "paths": [], "fingerprints": {},
                         "queue": [], "state": "active", "epoch": uuid.uuid4().hex}
    _save_native(vault, records)


def attention(vault: Path) -> dict:
    pending = []
    for identity, row in _native(vault).items():
        returned = row["state"] in _BLOCKED and any(_safe(vault, rel).exists() for rel in row["paths"])
        needs_restore = returned and all(rel in row.get("completed", []) for rel in row["paths"])
        if row["state"] in {"partial", "recycling", "metadata_pending"} or returned:
            pending.append({"id": row["id"], "paper_id": identity, "title": row["title"], "state": row["state"],
                            "kind": "restore" if needs_restore else "pending"})
    return {"legacy": list_entries(vault), "pending": pending}
