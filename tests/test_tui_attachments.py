"""Focused tests for native-Windows TUI attachment path parsing.

These tests pin the behavior that a POSIX/WSL path supplied by a user on native
Windows (``/home/...``, ``/mnt/...``, ``~/...`` and ``\\wsl.localhost\\...`` UNC
paths) resolves to a real readable file instead of being misinterpreted as a
current-drive Windows path.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tui.attachments import PendingAttachment, TuiAttachmentError, _native_path, snapshot_bytes, snapshot_path


def _snapshot(tmp_path: Path, value: str, **kwargs) -> PendingAttachment:
    return snapshot_path(
        value,
        generation=1,
        instance_id="HASHI_TEST",
        agent="lily",
        **kwargs,
    )


class _Completed:
    def __init__(self, stdout: str = ""):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def _wslpath_fake(wslpath_map: dict[str, str], home_windows: str):
    """Fake ``subprocess.run`` for the ``wsl.exe`` conversions used by the fix."""

    def fake_run(argv, **_kwargs):
        if argv and argv[0] == "wsl.exe":
            if "bash" in argv:
                return _Completed(home_windows)
            if "wslpath" in argv:
                posix = argv[-1]
                return _Completed(wslpath_map.get(posix, ""))
        raise AssertionError(f"unexpected subprocess.run argv: {argv!r}")

    return fake_run


def test_native_windows_file_snapshots(tmp_path):
    target = tmp_path / "hello.txt"
    target.write_text("hello", encoding="utf-8")
    pending = _snapshot(tmp_path, str(target))
    assert pending.filename == "hello.txt"
    assert pending.content == b"hello"


def test_directory_reports_an_actionable_next_step(tmp_path):
    with pytest.raises(TuiAttachmentError, match="select a file inside it"):
        _snapshot(tmp_path, str(tmp_path))


def test_quoted_path_is_accepted(tmp_path):
    target = tmp_path / "with space.txt"
    target.write_text("x", encoding="utf-8")
    pending = _snapshot(tmp_path, f'"{target}"')
    assert pending.filename == "with space.txt"


def test_missing_native_file_is_not_readable(tmp_path):
    with pytest.raises(TuiAttachmentError, match="readable"):
        _snapshot(tmp_path, str(tmp_path / "missing.txt"))


def test_size_limit_is_enforced(tmp_path):
    target = tmp_path / "big.txt"
    target.write_bytes(b"x" * 10)
    with pytest.raises(TuiAttachmentError, match="25 MiB"):
        _snapshot(tmp_path, str(target), max_bytes=5)


def test_snapshot_bytes_roundtrip():
    pending = snapshot_bytes(
        b"png-bytes",
        filename="shot.png",
        media_type="image/png",
        generation=1,
        instance_id="HASHI_TEST",
        agent="lily",
    )
    assert pending.filename == "shot.png"
    assert pending.content == b"png-bytes"


# --- native-Windows WSL/POSIX path normalization ---------------------------

_WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="native-Windows path parsing")


@_WINDOWS_ONLY
def test_posix_home_file_resolves_via_wslpath(tmp_path, monkeypatch):
    target = tmp_path / "home" / "lily" / "note.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hello wsl", encoding="utf-8")
    monkeypatch.setattr(
        subprocess,
        "run",
        _wslpath_fake({"/home/lily/note.txt": str(target)}, str(tmp_path / "home" / "lily")),
    )
    pending = _snapshot(tmp_path, "/home/lily/note.txt")
    assert pending.filename == "note.txt"
    assert pending.content == b"hello wsl"


@_WINDOWS_ONLY
def test_posix_mnt_drive_file_resolves_via_wslpath(tmp_path, monkeypatch):
    target = tmp_path / "drive_note.txt"
    target.write_text("hello drive", encoding="utf-8")
    monkeypatch.setattr(
        subprocess,
        "run",
        _wslpath_fake({"/mnt/c/Users/tester/drive_note.txt": str(target)}, str(tmp_path)),
    )
    pending = _snapshot(tmp_path, "/mnt/c/Users/tester/drive_note.txt")
    assert pending.filename == "drive_note.txt"
    assert pending.content == b"hello drive"


@_WINDOWS_ONLY
def test_tilde_file_resolves_to_wsl_home(tmp_path, monkeypatch):
    target = tmp_path / "home" / "lily" / "tildetest.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hello tilde", encoding="utf-8")
    monkeypatch.setattr(
        subprocess,
        "run",
        _wslpath_fake({}, str(tmp_path / "home" / "lily")),
    )
    pending = _snapshot(tmp_path, "~/tildetest.txt")
    assert pending.filename == "tildetest.txt"
    assert pending.content == b"hello tilde"


@_WINDOWS_ONLY
def test_missing_wsl_file_is_still_reported_not_readable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        _wslpath_fake({"/home/lily/missing.txt": str(tmp_path / "missing.txt")}, str(tmp_path)),
    )
    with pytest.raises(TuiAttachmentError, match="readable"):
        _snapshot(tmp_path, "/home/lily/missing.txt")


@_WINDOWS_ONLY
def test_unc_path_is_not_sent_through_wsl_conversion(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return _Completed("")

    monkeypatch.setattr(subprocess, "run", fake_run)
    raw = r"\\wsl.localhost\Ubuntu-22.04\home\lily\note.txt"
    path = _native_path(raw)
    assert calls == []
    assert str(path).replace("/", "\\").startswith("\\\\wsl.localhost\\")


_WSL_UNC_README = r"\\wsl.localhost\Ubuntu-22.04\home\lily\projects\hashi\README.md"


@_WINDOWS_ONLY
@pytest.mark.skipif(
    not Path(_WSL_UNC_README).is_file(),
    reason="WSL distro UNC path not available on this host",
)
def test_real_wsl_unc_file_snapshots():
    pending = snapshot_path(
        _WSL_UNC_README,
        generation=1,
        instance_id="HASHI_TEST",
        agent="lily",
    )
    assert pending.filename == "README.md"
    assert len(pending.content) > 0
