from __future__ import annotations

import json
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
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "hashi-test"\nversion = "4.1.2"\n'
    )
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


def test_authorized_core_change_still_requires_major_release_evidence(repository):
    (repository / "main.py").write_text("changed\n")
    git(repository, "add", "main.py")

    result = check(
        repository,
        "--cached",
        "--base",
        "HEAD",
        "--authorized",
        "--major-version-change",
    )

    assert result.returncode == 4
    assert "major-version increment" in result.stderr
    assert "independent review record" in result.stderr


def test_reviewed_major_core_change_passes(repository):
    (repository / "main.py").write_text("changed\n")
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "hashi-test"\nversion = "5.0.0a1"\n'
    )
    git(repository, "add", "main.py", "pyproject.toml")
    digest = check(repository, "--cached", "--print-core-digest").stdout.strip()
    review = repository / "docs" / "core-reviews" / "v5-review.json"
    review.parent.mkdir(parents=True)
    review.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "change_id": "core-v5-migration",
                "authorization_reference": "approved migration decision",
                "implementer": "implementer-agent",
                "reviewer": "reviewer-agent",
                "reviewed_at": "2026-09-13T23:30:00+10:00",
                "verdict": "approved",
                "product_version": "5.0.0a1",
                "core_digest": digest,
                "summary": "Reviewed the complete candidate Core diff and risks.",
            }
        )
    )
    git(repository, "add", str(review.relative_to(repository)))

    result = check(
        repository,
        "--cached",
        "--base",
        "HEAD",
        "--authorized",
        "--major-version-change",
    )

    assert result.returncode == 0, result.stderr
    assert "authorized major-version change" in result.stdout


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
