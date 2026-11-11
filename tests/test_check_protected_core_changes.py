from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from scripts import check_protected_core_changes as checker


def test_changed_files_supports_cached_and_base(monkeypatch) -> None:
    calls = []

    def fake_run(args, check, capture_output, text):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="main.py\nremote/main.py\n", stderr="")

    monkeypatch.setattr(checker.subprocess, "run", fake_run)
    args = argparse.Namespace(cached=True, base="main")

    changed = checker._changed_files(args)

    assert changed == {"main.py", "remote/main.py"}
    assert calls == [["git", "diff", "--name-only", "--no-renames", "--cached", "main", "--"]]


def test_changed_files_includes_untracked_new_core_files(monkeypatch) -> None:
    calls = []

    def fake_run(args, check, capture_output, text):
        calls.append(args)
        output = (
            "main.py\n"
            if args[:2] == ["git", "diff"]
            else "orchestrator/runtime_contract.py\n"
        )
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(checker.subprocess, "run", fake_run)
    args = argparse.Namespace(cached=False, base=None)

    changed = checker._changed_files(args)

    assert changed == {"main.py", "orchestrator/runtime_contract.py"}
    assert calls == [
        ["git", "diff", "--name-only", "--no-renames"],
        ["git", "diff", "--name-only", "--no-renames", "--cached"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]


def test_main_blocks_protected_paths_without_authorization(monkeypatch, capsys) -> None:
    monkeypatch.setattr(checker, "_repo_root", lambda: __import__("pathlib").Path("/tmp/repo"))
    monkeypatch.setattr(checker.os, "chdir", lambda path: None)
    monkeypatch.setattr(
        checker,
        "_changed_files",
        lambda args: {"orchestrator/runtime_contract.py", "remote/main.py"},
    )
    monkeypatch.delenv("HASHI_CORE_EDIT_AUTHORIZED", raising=False)

    result = checker.main([])

    captured = capsys.readouterr()
    assert result == 2
    assert "protected core check: blocked" in captured.err
    assert "orchestrator/runtime_contract.py" in captured.err
    assert "remote/main.py" not in captured.err


def test_explicit_authorization_alone_does_not_bypass_major_policy(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(checker, "_repo_root", lambda: __import__("pathlib").Path("/tmp/repo"))
    monkeypatch.setattr(checker.os, "chdir", lambda path: None)
    monkeypatch.setattr(checker, "_changed_files", lambda args: {"main.py"})

    assert checker.main(["--authorized"]) == 4
    assert "major-version migration" in capsys.readouterr().err


def test_protected_core_manifest_only_names_existing_files() -> None:
    root = Path(__file__).resolve().parent.parent

    assert checker._missing_manifest_paths(root) == []


def test_validate_manifest_fails_for_missing_path(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(checker, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(checker.os, "chdir", lambda path: None)
    monkeypatch.setattr(checker, "_changed_files", lambda args: set())

    result = checker.main(["--validate-manifest"])

    assert result == 3
    assert "manifest: invalid" in capsys.readouterr().err


@pytest.mark.parametrize("path_type", [PurePosixPath, PureWindowsPath])
@pytest.mark.parametrize("field", ["standard-lock", "core-host-lock"])
@pytest.mark.parametrize(
    "value",
    [
        "../outside.lock",
        r"..\outside.lock",
        r"nested\..\outside.lock",
        r"nested/..\outside.lock",
        r"nested\../outside.lock",
        r"nested\deeper/..\..\outside.lock",
    ],
)
def test_lock_parent_traversal_is_rejected_under_both_path_flavours(
    monkeypatch, path_type, field, value
):
    # TOML decoding precedes validation, including escaped Windows separators.
    source = "[tool.hashi.runtime]\n" + field + " = " + json.dumps(value) + "\n"
    monkeypatch.setattr(checker, "Path", path_type)
    with pytest.raises(ValueError, match="inside the repository"):
        checker._runtime_declarations(source)


@pytest.mark.parametrize("path_type", [PurePosixPath, PureWindowsPath])
@pytest.mark.parametrize("field", ["standard-lock", "core-host-lock"])
@pytest.mark.parametrize(
    "value",
    [
        "profile.lock",
        "constraints/nested/profile.lock",
        r"constraints\nested\profile.lock",
        r"constraints\nested/profile.lock",
        "constraints/release..lock",
    ],
)
def test_valid_relative_locks_are_accepted_under_both_path_flavours(
    monkeypatch, path_type, field, value
):
    source = "[tool.hashi.runtime]\n" + field + " = " + json.dumps(value) + "\n"
    monkeypatch.setattr(checker, "Path", path_type)
    checker._runtime_declarations(source)
# HASHI-Fingerprint: hfp_df6e1b14e1724365a4f5dacb3a867d52
