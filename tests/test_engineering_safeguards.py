from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

CHECKER = Path(__file__).resolve().parents[1] / "scripts/check_protected_core_changes.py"


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "main.py").write_text("original\n")
    folder = tmp_path / "orchestrator"
    folder.mkdir()
    (folder / "runtime_contract.py").write_text(
        'CORE_SOURCE_PATHS = ("main.py", "orchestrator/runtime_contract.py")\n'
    )
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline")
    return tmp_path


def check(root, *args):
    return subprocess.run([sys.executable, str(CHECKER), *args], cwd=root, capture_output=True, text=True)


def test_default_guard_detects_staged_core_change(repository):
    (repository / "main.py").write_text("changed\n")
    git(repository, "add", "main.py")
    result = check(repository)
    assert result.returncode == 2
    assert "main.py" in result.stderr


def test_old_manifest_still_protects_removed_entry(repository):
    (repository / "orchestrator/runtime_contract.py").write_text('CORE_SOURCE_PATHS = ()\n')
    (repository / "main.py").write_text("changed\n")
    result = check(repository, "--base", "HEAD")
    assert result.returncode == 2
    assert "main.py" in result.stderr


def test_cached_guard_uses_index_manifest(repository):
    (repository / "orchestrator/runtime_contract.py").write_text(
        'CORE_SOURCE_PATHS = ("main.py", "new_core.py")\n'
    )
    (repository / "new_core.py").write_text("protected addition\n")
    git(repository, "add", ".")
    (repository / "orchestrator/runtime_contract.py").write_text('CORE_SOURCE_PATHS = ()\n')
    result = check(repository, "--cached")
    assert result.returncode == 2
    assert "new_core.py" in result.stderr


def test_function_change_does_not_require_core_approval(repository):
    (repository / "feature.py").write_text("feature\n")
    git(repository, "add", "feature.py")
    assert check(repository, "--cached").returncode == 0


def test_installed_hook_blocks_core_commit_without_authorization(repository, monkeypatch):
    import shutil

    source = CHECKER.parents[1]
    (repository / "scripts").mkdir()
    shutil.copy2(CHECKER, repository / "scripts" / CHECKER.name)
    shutil.copytree(source / ".githooks", repository / ".githooks")
    git(repository, "config", "core.hooksPath", ".githooks")
    monkeypatch.setenv("HASHI_CHECK_PYTHON", sys.executable)
    monkeypatch.delenv("HASHI_CORE_EDIT_AUTHORIZED", raising=False)
    (repository / "main.py").write_text("changed\n")
    git(repository, "add", "main.py")
    result = subprocess.run(
        ["git", "-C", str(repository), "-c", "user.name=Test", "-c",
         "user.email=test@example.invalid", "commit", "-qm", "unapproved"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "main.py" in result.stderr
    assert git(repository, "log", "-1", "--format=%s").stdout.strip() == "baseline"
