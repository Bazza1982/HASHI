from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import pytest

pytestmark = pytest.mark.contract
ROOT = Path(__file__).resolve().parents[2]


def _git_paths(*args: str) -> set[str]:
    result = subprocess.run(
        ["git", *args, "-z"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return {
        value.decode("utf-8", "surrogateescape")
        for value in result.stdout.split(b"\0")
        if value
    }


def test_git_publication_tree_excludes_local_context_and_runtime_state() -> None:
    tracked = _git_paths("ls-files")
    forbidden_prefixes = (
        "flow/evaluation_kb/improvements/",
        "flow/evaluation_kb/workflow_scores/",
        "flow/evaluation_kb/workflow_versions/",
        "skills/agent-audit/",
        "skills/hermes-memory-import/",
        "skills/library-pick/",
        "skills/memory-consolidation/",
        "superloops/loops/",
        "superloops/recordings/",
        "workbench/",
        "workspaces/",
    )
    forbidden_files = {
        "fix_usb_path.bat",
        "scripts/check_stress_test.ps1",
        "scripts/start_stress_test.ps1",
        "windows/prepare_usb.bat",
        "windows/prepare_usb_international.bat",
    }

    offenders = {
        name
        for name in tracked
        if name in forbidden_files
        or name.startswith(forbidden_prefixes)
        or (
            (parts := PurePosixPath(name).parts)
            and parts[0] == "exp"
            and len(parts) >= 3
            and parts[1] != "examples"
        )
    }
    assert offenders == set()


def test_only_reviewed_templates_are_tracked_despite_ignore_rules() -> None:
    ignored_but_tracked = _git_paths(
        "ls-files",
        "-ci",
        "--exclude-standard",
    )

    assert ignored_but_tracked == {
        "packaging/portable_windows/requirements.lock",
        "packaging/portable_windows/templates/agent_capabilities.json",
        "packaging/portable_windows/templates/agents.json",
        "packaging/portable_windows/templates/api_gateway_state.json",
        "packaging/portable_windows/templates/tasks.json",
    }


def test_generated_typescript_outputs_are_not_tracked_as_editor_source() -> None:
    tracked = _git_paths("ls-files")
    generated = {
        name
        for name in tracked
        if name in {"nagare-viz/vite.config.js", "nagare-viz/vite.config.d.ts"}
        or (
            name.startswith("nagare-viz/src/")
            and name.endswith((".js", ".d.ts"))
        )
    }

    assert generated == set()


def test_tracked_text_has_no_operator_absolute_paths() -> None:
    tracked = _git_paths("ls-files")
    text_suffixes = {
        ".bat",
        ".cjs",
        ".cmd",
        ".js",
        ".json",
        ".jsonl",
        ".md",
        ".ps1",
        ".py",
        ".sh",
        ".toml",
        ".yaml",
        ".yml",
    }
    forbidden = (
        b"/home/" + b"lily/",
        b"/mnt/c/Users/" + b"thene/",
        b"C:/Users/" + b"thene/",
        b"C:\\Users\\" + b"thene\\",
        b"C:\\\\Users\\\\" + b"thene\\\\",
        b"C:\\Users\\" + b"lily\\",
        b"C:\\\\Users\\\\" + b"lily\\\\",
        b"a9" + b"max",
        b"192.168.0." + b"211",
        b"192.168.0." + b"41",
        b"192.168.0." + b"6",
    )
    offenders = set()
    for name in tracked:
        if PurePosixPath(name).suffix.lower() not in text_suffixes:
            continue
        result = subprocess.run(
            ["git", "show", f":{name}"],
            cwd=ROOT,
            check=False,
            capture_output=True,
        )
        assert result.returncode == 0, name
        content = result.stdout.lower()
        if any(marker.lower() in content for marker in forbidden):
            offenders.add(name)

    assert offenders == set()
