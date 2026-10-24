"""Opt-in native probe; only recycle and restore files created by this invocation."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, cast

import test_app_backend as backend

import windows_recycle as native


def restore_owned(completed: dict[str, str], owned_root: Path) -> None:
    import pythoncom
    from win32com.shell import shell

    for original in completed:
        path = Path(original)
        if not path.is_relative_to(owned_root.resolve()) or path.exists():
            raise ValueError("Refusing restore outside this probe or over an existing destination")
    if not completed:
        return
    pythoncom.CoInitialize()
    try:
        operation = cast(Any, pythoncom.CoCreateInstance(shell.CLSID_FileOperation, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IFileOperation))
        operation.SetOperationFlags(native.NO_CONFIRMATION | native.NO_ERROR_UI | native.SILENT | native.EARLY_FAILURE)
        for original, recycled in completed.items():
            destination = Path(original)
            item = shell.SHCreateItemFromParsingName(recycled, pythoncom.CreateBindCtx(), shell.IID_IShellItem)
            parent = shell.SHCreateItemFromParsingName(str(destination.parent), pythoncom.CreateBindCtx(), shell.IID_IShellItem)
            operation.MoveItem(item, parent, destination.name, None)
        result = operation.PerformOperations()
        if result or operation.GetAnyOperationsAborted():
            raise OSError("Restoring the probe's own items did not complete")
    finally:
        pythoncom.CoUninitialize()


def main():
    root = backend.new_vault("windows-recycle-probe").resolve()
    marker = "lazyscholar-probe-" + uuid.uuid4().hex
    file = root / (marker + ".txt")
    directory = root / (marker + "-cache")
    directory.mkdir()
    child = directory / "payload.txt"
    original = b"Only this invocation owns this reversible recycle probe.\n"
    file.write_bytes(original)
    child.write_bytes(original + b"nested cache\n")
    result = native.Result()
    report: dict[str, Any] = {"result": "FAIL", "run": str(root)}
    try:
        result = native.recycle([file, directory])
        report["completed"] = len(result.completed)
        report["failed"] = len(result.failed)
        report["aborted"] = result.aborted
        if not result.succeeded([file, directory]):
            raise AssertionError("Windows did not confirm every probe item entered its Recycle Bin: " + result.error)
        if file.exists() or directory.exists():
            raise AssertionError("Probe originals still exist after native recycling")
        report["native_recycle"] = "PASS"
    finally:
        restore_owned(result.completed, root)
        report["restored"] = file.exists() and child.exists() and file.read_bytes() == original and child.read_bytes() == original + b"nested cache\n"
        if report.get("native_recycle") == "PASS" and report["restored"]:
            report["result"] = "PASS"
        (root / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2), flush=True)
    if report["result"] != "PASS":
        raise AssertionError("Native recycle/restore probe failed")


if __name__ == "__main__":
    main()
