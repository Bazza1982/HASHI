"""Observe the mandatory integrity label on real disposable Windows scratch."""
import ctypes
from ctypes import wintypes
import sys

import pytest

from orchestrator.permissions.windows_container import protect_task_scratch
from orchestrator.permissions.windows_ipc import current_process_user_sid


def label(path):
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = wintypes.LPVOID
    security.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
                                              pointer, pointer, pointer, pointer, ctypes.POINTER(pointer)]
    security.GetNamedSecurityInfoW.restype = wintypes.DWORD
    security.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        pointer, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), pointer]
    security.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [pointer], pointer
    descriptor, text = pointer(), wintypes.LPWSTR()
    code = security.GetNamedSecurityInfoW(str(path), 1, 0x10, None, None, None, None, ctypes.byref(descriptor))
    if code:
        raise ctypes.WinError(code)
    try:
        if not security.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 0x10, ctypes.byref(text), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return text.value
    finally:
        if text:
            kernel.LocalFree(text)
        kernel.LocalFree(descriptor)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows mandatory integrity label")
def test_task_scratch_is_low_integrity_without_relabeling_its_parent(tmp_path):
    parent_label = label(tmp_path)
    task = tmp_path / "task"
    task.mkdir()
    protect_task_scratch(task, runner_sid=current_process_user_sid(), appcontainer_sid="S-1-15-2-1-2-3")
    assert ";;;LW)" in label(task)
    child = task / "output.txt"
    child.write_text("owned temporary output", encoding="utf-8")
    assert ";;;LW)" in label(child)
    assert label(tmp_path) == parent_label
