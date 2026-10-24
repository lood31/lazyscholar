"""Windows Recycle Bin only: no permanent-delete or legacy API fallback."""
from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, cast

RECYCLE_ON_DELETE = 0x00080000
ADD_UNDO_RECORD = 0x20000000
EARLY_FAILURE = 0x00100000
NO_CONFIRMATION = 0x0010
NO_ERROR_UI = 0x0400
SILENT = 0x0004
RECYCLE_FLAGS = RECYCLE_ON_DELETE | ADD_UNDO_RECORD | EARLY_FAILURE | NO_CONFIRMATION | NO_ERROR_UI | SILENT
RECYCLE_TRANSFER = 0x80  # TSF_DELETE_RECYCLE_IF_POSSIBLE


@dataclass
class Result:
    completed: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    aborted: bool = False
    error: str = ""

    def succeeded(self, paths: list[Path]) -> bool:
        completed = {Path(path) for path in self.completed}
        return not self.error and not self.aborted and not self.failed and all(path in completed for path in paths)


def require_recyclable(flags: int) -> None:
    if not flags & RECYCLE_TRANSFER:
        raise OSError("Windows 未确认可以回收，已阻止操作；不会永久删除")


def validate(paths: list[Path]) -> list[Path]:
    if os.name != "nt" or tuple(sys.getwindowsversion()[:2]) < (6, 2):
        raise OSError("系统回收功能需要 Windows 8 或更新版本；未移动文件")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    kernel32.GetDriveTypeW.restype = ctypes.c_uint
    resolved = []
    for source in paths:
        source = Path(source)
        if not source.is_absolute() or str(source).startswith("\\\\"):
            raise ValueError("系统回收只支持本地固定磁盘上的明确绝对路径")
        for parent in (source, *source.parents):
            if parent.is_symlink() or parent.is_junction():
                raise ValueError("不回收符号链接或目录联接")
        path = source.resolve(strict=True)
        if kernel32.GetDriveTypeW(path.anchor) != 3:  # DRIVE_FIXED
            raise OSError("该存储卷不支持本应用的安全回收；未移动文件")
        if path.is_dir():
            for child in path.rglob("*"):
                if child.is_symlink() or child.is_junction():
                    raise ValueError("目录含有符号链接或目录联接，未移动文件")
        if path in resolved or any(path.is_relative_to(other) or other.is_relative_to(path) for other in resolved):
            raise ValueError("回收路径重复或嵌套，未移动文件")
        resolved.append(path)
    return resolved


def recycle(paths: list[Path]) -> Result:
    """Return per-root results, including partial success. All validation precedes COM."""
    paths = validate(paths)
    if not paths:
        return Result()
    try:
        import pythoncom
        from win32com.server.exception import COMException
        from win32com.server.policy import DesignatedWrapPolicy
        from win32com.shell import shell
    except ImportError as exc:
        raise OSError("Windows 回收组件缺失，未移动文件；不会改用永久删除") from exc

    result = Result()
    noops = ("StartOperations", "FinishOperations", "PreRenameItem", "PostRenameItem", "PreMoveItem", "PostMoveItem",
             "PreCopyItem", "PostCopyItem", "PreNewItem", "PostNewItem", "UpdateProgress", "ResetTimer", "PauseTimer", "ResumeTimer")

    def noop(*_args):
        return 0

    class Sink(DesignatedWrapPolicy):
        _com_interfaces_: ClassVar[list] = [shell.IID_IFileOperationProgressSink]
        _public_methods_: ClassVar[list[str]] = [*noops, "PreDeleteItem", "PostDeleteItem"]

        def __init__(self, source):
            self.source = source
            cast(Any, self)._wrap_(self)

        def PreDeleteItem(self, flags, item):
            try:
                require_recyclable(flags)
            except OSError as exc:
                result.failed[str(self.source)] = str(exc)
                raise COMException(str(exc), scode=-2147467259) from exc
            return 0

        def PostDeleteItem(self, flags, item, status, newly_created):
            # Each DeleteItem has its own sink. The original IShellItem can track
            # its new location, so retain the bound source identity, not its display name.
            if status & 0x80000000 or newly_created is None:
                result.failed[str(self.source)] = "系统未确认该项目已进入回收站"
            else:
                result.completed[str(self.source)] = newly_created.GetDisplayName(0x80028000)  # DESKTOPABSOLUTEPARSING
            return 0

    for name in noops:
        setattr(Sink, name, noop)

    initialized = False
    operation = item = sink = None
    sinks = []
    try:
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        initialized = True
        # pywin32's stubs expose IUnknown; these methods belong to the requested COM interface.
        operation = cast(Any, pythoncom.CoCreateInstance(shell.CLSID_FileOperation, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IFileOperation))
        operation.SetOperationFlags(RECYCLE_FLAGS)
        for path in paths:
            sink = cast(Any, pythoncom.WrapObject)(Sink(path), shell.IID_IFileOperationProgressSink)
            sinks.append(sink)
            item = shell.SHCreateItemFromParsingName(str(path), pythoncom.CreateBindCtx(), shell.IID_IShellItem)
            operation.DeleteItem(item, sink)
        status = operation.PerformOperations()
        result.aborted = bool(operation.GetAnyOperationsAborted())
        if status or result.aborted:
            result.error = "Windows 回收操作未完整完成"
        for path in paths:
            if str(path) not in result.completed and str(path) not in result.failed:
                result.failed[str(path)] = "系统未返回该项目的回收确认"
    except (pythoncom.com_error, OSError, ValueError) as exc:
        # Per-item successes must survive a later COM failure; never emulate rollback by deleting.
        result.error = str(exc)
    finally:
        operation = item = sink = None
        sinks.clear()
        if initialized:
            pythoncom.CoUninitialize()
    return result
