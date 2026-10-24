"""Native adapter contracts without performing file operations in Windows."""
from __future__ import annotations

import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_app_backend as backend

import windows_recycle as native


@unittest.skipUnless(sys.platform == "win32", "Windows COM adapter")
class WindowsRecycleTests(unittest.TestCase):
    def setUp(self):
        self.root = backend.new_vault(self._testMethodName).resolve()
        self.paths = [self.root / "one.txt", self.root / "two.txt"]
        for path in self.paths:
            path.write_bytes(b"owned unit fixture, never actually deleted")

    def call(self, *, flags=native.RECYCLE_TRANSFER, fail_after=None, aborted=False, init_failure=False, status=0):
        import pythoncom
        from win32com.server.exception import COMException
        from win32com.shell import shell

        queued, seen_flags = [], []
        def perform():
            for index, (item, sink) in enumerate(queued):
                if fail_after is not None and index == fail_after:
                    raise pythoncom.com_error(-2147467259, "simulated COM refusal", None, None)
                try:
                    sink.PreDeleteItem(flags, item)
                except COMException as exc:
                    raise pythoncom.com_error(-2147467259, "not recyclable", None, None) from exc
                sink.PostDeleteItem(flags, item, status, SimpleNamespace(GetDisplayName=lambda kind: r"C:\$Recycle.Bin\owned-unit-item"))
        operation = SimpleNamespace(SetOperationFlags=seen_flags.append, DeleteItem=lambda item,sink:queued.append((item,sink)),
                                    PerformOperations=perform, GetAnyOperationsAborted=lambda:aborted)
        with ExitStack() as stack:
            stack.enter_context(patch.object(native,"validate",return_value=self.paths))
            stack.enter_context(patch.object(pythoncom,"CoInitializeEx",side_effect=pythoncom.com_error(-2147467259,"init failed",None,None) if init_failure else None))
            uninitialize = stack.enter_context(patch.object(pythoncom,"CoUninitialize"))
            stack.enter_context(patch.object(pythoncom,"CoCreateInstance",return_value=operation))
            stack.enter_context(patch.object(pythoncom,"WrapObject",side_effect=lambda value, iid:value))
            # The item identity need not remain at the original path after a move.
            stack.enter_context(patch.object(shell,"SHCreateItemFromParsingName",return_value=SimpleNamespace()))
            with patch.object(Path,"unlink",side_effect=AssertionError("permanent delete forbidden")):
                result = native.recycle(self.paths)
            calls = uninitialize.call_count
        self.assertTrue(all(path.exists() for path in self.paths))
        return result, seen_flags, calls

    def test_native_flags_require_recycle_undo_early_failure_and_no_native_confirmation(self):
        result, flags, calls = self.call()
        self.assertTrue(result.succeeded(self.paths))
        self.assertEqual([native.RECYCLE_FLAGS],flags)
        for required in (native.RECYCLE_ON_DELETE,native.ADD_UNDO_RECORD,native.EARLY_FAILURE,native.NO_ERROR_UI,native.NO_CONFIRMATION):
            self.assertTrue(flags[0]&required)
        self.assertEqual(1,calls)

    def test_non_recyclable_predelete_refuses_before_any_success(self):
        result, _flags, _calls = self.call(flags=0)
        self.assertFalse(result.succeeded(self.paths))
        self.assertEqual({},result.completed)
        self.assertTrue(result.failed)

    def test_partial_com_failure_retains_per_item_success(self):
        result, _flags, _calls = self.call(fail_after=1)
        self.assertFalse(result.succeeded(self.paths))
        self.assertEqual([str(self.paths[0])],list(result.completed))
        self.assertTrue(result.error)

    def test_aborted_operation_is_not_success(self):
        result, _flags, _calls = self.call(aborted=True)
        self.assertFalse(result.succeeded(self.paths))
        self.assertTrue(result.aborted)

    def test_success_hresult_need_not_be_exact_s_ok(self):
        result, _flags, _calls = self.call(status=0x00270003)
        self.assertTrue(result.succeeded(self.paths))

    def test_failed_apartment_initialization_does_not_uninitialize_someone_else(self):
        result, _flags, calls = self.call(init_failure=True)
        self.assertFalse(result.succeeded(self.paths))
        self.assertEqual(0,calls)

    def test_duplicate_nested_relative_and_unc_targets_are_rejected(self):
        for paths in ([self.paths[0],self.paths[0]],[self.root,self.paths[0]],[Path("relative.txt")],[Path(r"\\server\share\paper.pdf")]):
            with self.assertRaises((ValueError,OSError)):
                native.validate(paths)


if __name__ == "__main__":
    unittest.main()
