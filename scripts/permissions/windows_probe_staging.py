"""Stage an isolated Python runtime for a disposable Windows permission probe.

This is opt-in test infrastructure, not an installer or a security service.
The caller chooses an isolated parent directory and must use a disposable,
non-administrator account. No account, process or profile is created here.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import uuid


_SID = re.compile(r"S-1-5-21-(?:[0-9]+-){3}[0-9]+\Z")
_MARKER = ".hashi-test-stage.json"


class StageError(RuntimeError):
    """The test directory cannot be staged or safely cleaned."""


def _reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _digest(path: Path) -> bytes:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.digest()


def _copy_file(source: Path, destination: Path) -> None:
    if _reparse(source) or not source.is_file():
        raise StageError("test runtime contains an unsupported file")
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer)
    if _digest(source) != _digest(destination):
        raise StageError("staged runtime file differs from its source")


def _copy_tree(source: Path, destination: Path) -> None:
    if _reparse(source) or not source.is_dir():
        raise StageError("test runtime directory is unavailable")
    destination.mkdir()
    for child in source.iterdir():
        if child.name in {"site-packages", "__pycache__"}:
            continue
        if _reparse(child):
            raise StageError("test runtime contains a reparse point")
        target = destination / child.name
        if child.is_dir():
            _copy_tree(child, target)
        elif child.is_file():
            _copy_file(child, target)
        else:
            raise StageError("test runtime contains an unsupported object")


class WindowsStageACL:
    """Apply ACLs and hold only the unique test directory created by the caller."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise StageError("native Windows is required")
        self.adv = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        self.adv.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
        self.adv.SetFileSecurityW.restype = wintypes.BOOL
        self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel.LocalFree.restype = ctypes.c_void_p
        self.kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL

    def hold(self, path: Path):
        # Refuse delete sharing so the directory name cannot be replaced while
        # its contents are being prepared or checked.
        handle = self.kernel.CreateFileW(str(path), 0x00100080, 3, None, 3, 0x02200000, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise StageError("cannot hold the test directory")
        return handle

    def close(self, handle) -> None:
        if not self.kernel.CloseHandle(handle):
            raise StageError("test directory handle cleanup is unverified")

    def apply(self, path: Path, sid: str, *, writable: bool) -> None:
        if not isinstance(sid, str) or not _SID.fullmatch(sid):
            raise StageError("a disposable local account SID is required")
        access = "FA" if writable else "0x1200a9"
        sddl = f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;{access};;;{sid})"
        descriptor = ctypes.c_void_p()
        if not self.adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        ):
            raise StageError("cannot build the test directory ACL")
        try:
            if not self.adv.SetFileSecurityW(str(path), 0x80000004, descriptor):
                raise StageError("cannot set the test directory ACL")
        finally:
            self.kernel.LocalFree(descriptor)


class DisposableProbeStage:
    """Own a uniquely named, verified staging tree and remove only that tree."""

    def __init__(self, parent: Path, sid: str, python_root: Path, probe: Path, acl) -> None:
        self.parent = Path(parent)
        self.sid = sid
        self.python_root = Path(python_root)
        self.probe = Path(probe)
        self.acl = acl
        if (not self.parent.is_absolute() or not self.parent.is_dir()
                or not self.python_root.is_absolute() or not self.probe.is_absolute()
                or not isinstance(sid, str) or not _SID.fullmatch(sid)):
            raise StageError("invalid isolated test staging parameters")
        self.identifier = uuid.uuid4().hex
        self.root = self.parent / ("HashiProbeStage_" + self.identifier)
        self.python = self.root / "python"
        self.scratch = self.root / "scratch"
        self._identity = None
        self._handle = None

    def __enter__(self):
        self.root.mkdir()
        info = self.root.stat()
        self._identity = (info.st_dev, info.st_ino)
        marker_written = False
        try:
            self._handle = self.acl.hold(self.root)
            self.acl.apply(self.root, self.sid, writable=False)
            with (self.root / _MARKER).open("x", encoding="utf-8") as marker:
                json.dump({"id": self.identifier, "sid": self.sid}, marker, sort_keys=True)
                marker.flush()
                os.fsync(marker.fileno())
            marker_written = True
            self.python.mkdir()
            major, minor = sys.version_info[:2]
            required = ("python.exe", f"python{major}{minor}.dll")
            optional = ("python3.dll", "vcruntime140.dll", "vcruntime140_1.dll")
            for name in required:
                _copy_file(self.python_root / name, self.python / name)
            for name in optional:
                candidate = self.python_root / name
                if candidate.exists():
                    _copy_file(candidate, self.python / name)
            for name in ("Lib", "DLLs"):
                _copy_tree(self.python_root / name, self.python / name)
            _copy_file(self.probe, self.root / "probe.py")
            self.scratch.mkdir()
            self.acl.apply(self.scratch, self.sid, writable=True)
            return self
        except BaseException:
            if marker_written:
                self.cleanup()
            else:
                if self._handle is not None:
                    self.acl.close(self._handle)
                    self._handle = None
                # Before the marker exists, remove only the still-empty
                # directory with the exact identity this object created.
                self._check_identity()
                self.root.rmdir()
            raise

    def _check_identity(self) -> None:
        if self._identity is None or _reparse(self.root):
            raise StageError("test directory identity is not proven")
        info = self.root.stat()
        if (info.st_dev, info.st_ino) != self._identity:
            raise StageError("test directory was replaced")

    def cleanup(self) -> None:
        self._check_identity()
        try:
            value = json.loads((self.root / _MARKER).read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise StageError("test directory ownership record is unavailable") from exc
        if value != {"id": self.identifier, "sid": self.sid}:
            raise StageError("test directory ownership record changed")
        if self._handle is not None:
            self.acl.close(self._handle)
            self._handle = None
        shutil.rmtree(self.root)
        if self.root.exists():
            raise StageError("test directory cleanup is unverified")

    def __exit__(self, exc_type, exc, traceback):
        self.cleanup()
