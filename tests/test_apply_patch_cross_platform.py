"""Cross-platform ``apply_patch`` discovery, fallbacks and diff application."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import tools.builtins as builtins
from tools.builtins import execute_apply_patch

SIMPLE_DIFF = (
    "--- a/notes.txt\n"
    "+++ b/notes.txt\n"
    "@@ -1,3 +1,3 @@\n"
    " one\n"
    "-two\n"
    "+TWO\n"
    " three\n"
)
BASE_TEXT = "one\ntwo\nthree\n"


def _write(tmp_path: Path, name: str = "notes.txt", text: str = BASE_TEXT) -> Path:
    target = tmp_path / name
    with target.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return target


def _read(target: Path) -> str:
    with target.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


async def _apply(tmp_path, patch: str = SIMPLE_DIFF, name: str = "notes.txt") -> str:
    return await execute_apply_patch(
        {"path": name, "patch": patch},
        access_root=tmp_path,
        workspace_dir=tmp_path,
    )


# --- discovery ------------------------------------------------------------


def test_candidate_patch_commands_prefers_path(monkeypatch) -> None:
    monkeypatch.setattr(
        builtins.shutil,
        "which",
        lambda name, *args, **kwargs: "/usr/bin/patch" if name == "patch" else None,
    )
    candidates = builtins._candidate_patch_commands()
    assert candidates[0] == "/usr/bin/patch"


def test_windows_patch_candidates_derives_git_usr_bin() -> None:
    candidates = builtins._windows_patch_candidates(
        r"C:\Program Files\Git\cmd\git.exe",
        {"ProgramFiles": r"C:\Program Files"},
    )
    normalized = [candidate.replace("\\", "/") for candidate in candidates]
    assert any(candidate.endswith("Git/usr/bin/patch.exe") for candidate in normalized)


def test_windows_patch_candidates_uses_localappdata() -> None:
    candidates = builtins._windows_patch_candidates(
        None, {"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}
    )
    normalized = [candidate.replace("\\", "/") for candidate in candidates]
    assert any(
        candidate.endswith("Programs/Git/usr/bin/patch.exe") for candidate in normalized
    )


def test_find_patch_command_returns_first_existing(tmp_path, monkeypatch) -> None:
    real = tmp_path / "patch.bin"
    real.write_text("x", encoding="utf-8")
    missing = tmp_path / "nope" / "patch"
    monkeypatch.setattr(
        builtins, "_candidate_patch_commands", lambda: [str(missing), str(real)]
    )
    assert builtins._find_patch_command() == str(real)


def test_find_patch_command_none_when_no_candidate_exists(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        builtins, "_candidate_patch_commands", lambda: [str(tmp_path / "absent")]
    )
    assert builtins._find_patch_command() is None


# --- header retargeting ---------------------------------------------------


def test_retarget_patch_headers_replaces_first_pair() -> None:
    original = (
        "diff --git a/x b/x\n"
        "--- a/x\n"
        "+++ b/x\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
    )
    out = builtins._retarget_patch_headers(original, "y.txt")
    assert "--- a/y.txt" in out
    assert "+++ b/y.txt" in out
    assert out.count("--- ") == 1
    assert "@@ -1 +1 @@" in out


# --- tier precedence ------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_command_tier_wins_and_blocks_fallbacks(tmp_path, monkeypatch) -> None:
    _write(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(builtins, "_find_patch_command", lambda: "/usr/bin/patch")

    def fake_command(patch_cmd, path, patch_str):
        calls.append("command")
        return ("ok", "patching file notes.txt")

    def fail(*args, **kwargs):
        calls.append("fallback")
        raise AssertionError("fallback tier must not run")

    monkeypatch.setattr(builtins, "_apply_patch_with_patch_command", fake_command)
    monkeypatch.setattr(builtins, "_apply_patch_with_git", fail)
    monkeypatch.setattr(builtins, "_apply_patch_with_python", fail)

    result = await _apply(tmp_path)
    assert result.startswith("OK: patch applied to")
    assert calls == ["command"]


@pytest.mark.asyncio
async def test_patch_command_rejection_is_authoritative(tmp_path, monkeypatch) -> None:
    target = _write(tmp_path)
    monkeypatch.setattr(builtins, "_find_patch_command", lambda: "patch")
    monkeypatch.setattr(
        builtins,
        "_apply_patch_with_patch_command",
        lambda *args, **kwargs: ("rejected", "hunk failed"),
    )

    def fail(*args, **kwargs):
        raise AssertionError("must not fall back after a rejection")

    monkeypatch.setattr(builtins, "_apply_patch_with_git", fail)
    monkeypatch.setattr(builtins, "_apply_patch_with_python", fail)

    result = await _apply(tmp_path)
    assert result == "Error: patch rejected (dry-run):\nhunk failed"
    assert _read(target) == BASE_TEXT


@pytest.mark.asyncio
async def test_git_tier_used_when_patch_missing(tmp_path, monkeypatch) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    target = _write(tmp_path)
    monkeypatch.setattr(builtins, "_find_patch_command", lambda: None)

    result = await _apply(tmp_path)
    assert result.startswith("OK: patch applied to")
    assert _read(target) == "one\nTWO\nthree\n"


@pytest.mark.asyncio
async def test_git_tier_targets_requested_file_despite_header(
    tmp_path, monkeypatch
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    target = _write(tmp_path, name="real.txt")
    foreign = (
        "--- a/something_else.txt\n"
        "+++ b/something_else.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " one\n"
        "-two\n"
        "+TWO\n"
        " three\n"
    )
    monkeypatch.setattr(builtins, "_find_patch_command", lambda: None)

    result = await _apply(tmp_path, patch=foreign, name="real.txt")
    assert result.startswith("OK: patch applied to")
    assert _read(target) == "one\nTWO\nthree\n"


@pytest.mark.asyncio
async def test_python_tier_used_when_patch_and_git_missing(tmp_path, monkeypatch) -> None:
    target = _write(tmp_path)
    monkeypatch.setattr(builtins, "_find_patch_command", lambda: None)
    monkeypatch.setattr(builtins.shutil, "which", lambda *args, **kwargs: None)

    result = await _apply(tmp_path)
    assert result.startswith("OK: patch applied to")
    assert _read(target) == "one\nTWO\nthree\n"


@pytest.mark.asyncio
async def test_all_tiers_missing_rejection_leaves_file_unchanged(
    tmp_path, monkeypatch
) -> None:
    target = _write(tmp_path)
    monkeypatch.setattr(builtins, "_find_patch_command", lambda: None)
    monkeypatch.setattr(builtins.shutil, "which", lambda *args, **kwargs: None)
    bad = (
        "--- a/notes.txt\n"
        "+++ b/notes.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " one\n"
        "-MISSING\n"
        "+X\n"
        " three\n"
    )

    result = await _apply(tmp_path, patch=bad)
    assert result.startswith("Error: patch rejected (dry-run):")
    assert _read(target) == BASE_TEXT


# --- pure-Python diff engine ---------------------------------------------


def test_pure_python_multi_hunk_applies_with_offsets() -> None:
    text = "a\nb\nc\nd\ne\nf\n"
    patch = (
        "--- a/f\n"
        "+++ b/f\n"
        "@@ -1,2 +1,3 @@\n"
        " a\n"
        "-b\n"
        "+B1\n"
        "+B2\n"
        "@@ -5,2 +6,2 @@\n"
        " e\n"
        "-f\n"
        "+F\n"
    )
    ok, out = builtins._apply_unified_diff(text, patch)
    assert ok is True
    assert out == "a\nB1\nB2\nc\nd\ne\nF\n"


def test_pure_python_preserves_crlf() -> None:
    text = "a\r\nb\r\nc\r\n"
    patch = "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
    ok, out = builtins._apply_unified_diff(text, patch)
    assert ok is True
    assert out == "a\r\nB\r\nc\r\n"


def test_pure_python_rejects_context_mismatch() -> None:
    ok, message = builtins._apply_unified_diff(
        "one\ntwo\nthree\n",
        "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n one\n-XX\n+YY\n three\n",
    )
    assert ok is False
    assert "does not match" in message


def test_pure_python_without_trailing_newline() -> None:
    ok, out = builtins._apply_unified_diff(
        "one\ntwo\nthree",
        "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n",
    )
    assert ok is True
    assert out == "one\nTWO\nthree"


# --- end-to-end with the real ``patch`` binary (when present) -------------


@pytest.mark.asyncio
async def test_end_to_end_with_real_patch_when_available(tmp_path) -> None:
    if builtins._find_patch_command() is None:
        pytest.skip("no patch executable located")
    target = _write(tmp_path)
    result = await _apply(tmp_path)
    assert result.startswith("OK: patch applied to")
    assert _read(target) == "one\nTWO\nthree\n"
