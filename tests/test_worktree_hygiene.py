"""Guardrail: runtime artifacts stay ignored and never pollute a clean git worktree."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

RUNTIME_ARTIFACT_PROBES = (
    "secrets.json",
    "agents.json",
    "tasks.json",
    "scheduler_state.json",
    "api_gateway_state.json",
    "logs/runtime.log",
    "state/hal/hal_v1.sqlite",
    "state/hal/hal_v1.sqlite-wal",
    "hal/hal_v1.sqlite",
    "hal/hal_v1.sqlite-shm",
    "workspaces/demo/bridge_memory.sqlite",
    "workspaces/demo/state.json",
    "workspaces/demo/conversation_log.jsonl",
    ".bridge_u_f.pid",
    "media/demo/voice.ogg",
    "__pycache__/probe.pyc",
    ".venv/pyvenv.cfg",
)

FORBIDDEN_TRACKED_EXTENSIONS = (".sqlite", ".sqlite-wal", ".sqlite-shm", ".pid", ".pyc")
FORBIDDEN_TRACKED_BASENAMES = frozenset(
    {
        "secrets.json",
        "agents.json",
        "tasks.json",
        "scheduler_state.json",
        "api_gateway_state.json",
    }
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_gitignore_blocks_runtime_artifacts_from_polluting_worktree():
    probe = _git("rev-parse", "--is-inside-work-tree")
    if probe.returncode != 0:
        pytest.skip("not running inside a git worktree")

    unignored: list[str] = []
    for rel_path in RUNTIME_ARTIFACT_PROBES:
        check = _git("check-ignore", "--no-index", "-q", rel_path)
        if check.returncode != 0:
            unignored.append(rel_path)

    assert not unignored, (
        f"Runtime artifacts are not covered by .gitignore and could pollute git status: {unignored}"
    )


def test_no_runtime_databases_or_secret_files_are_tracked_in_git():
    ls = _git("ls-files")
    if ls.returncode != 0:
        pytest.skip("not running inside a git worktree")

    tracked = [line.strip() for line in ls.stdout.splitlines() if line.strip()]
    violations: list[str] = []
    for path in tracked:
        name = Path(path).name
        if path.endswith(FORBIDDEN_TRACKED_EXTENSIONS):
            violations.append(path)
        elif name in FORBIDDEN_TRACKED_BASENAMES and "/" not in path:
            violations.append(path)

    assert not violations, f"Runtime or secret files must never be tracked in git: {violations}"
