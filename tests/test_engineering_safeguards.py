from __future__ import annotations

import hashlib
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
                "reviewed_files_sha256": {
                    "pyproject.toml": "sha256:" + hashlib.sha256(
                        (repository / "pyproject.toml").read_text().encode()
                    ).hexdigest(),
                },
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


def _runtime_baseline(root):
    metadata = root / "pyproject.toml"
    metadata.write_text(
        metadata.read_text()
        + '\n[tool.hashi.runtime]\nstandard-lock = "constraints/declared.lock"\n'
    )
    lock = root / "constraints" / "declared.lock"
    lock.parent.mkdir()
    lock.write_text("example-package==1.0\n")
    git(root, "add", ".")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "runtime baseline")
    return metadata, lock


@pytest.mark.parametrize(
    "change",
    ["major_up", "major_down", "pin", "comment", "redirect",
     "same_file_dot", "same_file_backslash", "same_file_slash"],
)
def test_runtime_generation_without_core_source_edits_requires_approval(repository, change):
    metadata, lock = _runtime_baseline(repository)
    if change.startswith("major_"):
        version = "5.0.0a1" if change == "major_up" else "3.0.0a1"
        metadata.write_text(metadata.read_text().replace("4.1.2", version))
    elif change.startswith("same_file_"):
        owner = {
            "same_file_dot": "./constraints/declared.lock",
            "same_file_backslash": "constraints\\declared.lock",
            "same_file_slash": "constraints/declared.lock/",
        }[change]
        metadata.write_text(
            metadata.read_text().replace('"constraints/declared.lock"', json.dumps(owner))
        )
    elif change == "redirect":
        replacement = lock.with_name("replacement.lock")
        replacement.write_text(lock.read_text())
        metadata.write_text(
            metadata.read_text().replace("constraints/declared.lock", "constraints/replacement.lock")
        )
    else:
        lock.write_text("example-package==2.0\n" if change == "pin" else lock.read_text() + "# new generation\n")
    git(repository, "add", ".")
    result = check(repository, "--cached")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "core-change-approved" in result.stderr


def test_staged_major_change_cannot_be_hidden_by_restoring_worktree_metadata(repository):
    metadata, _ = _runtime_baseline(repository)
    original = metadata.read_text()
    metadata.write_text(original.replace("4.1.2", "5.0.0a1"))
    git(repository, "add", "pyproject.toml")
    metadata.write_text(original)
    assert check(repository).returncode == 2
    assert check(repository, "--cached").returncode == 2


def test_removing_lock_owner_cannot_hide_a_staged_dependency_change(repository):
    metadata, lock = _runtime_baseline(repository)
    lock.write_text("example-package==2.0\n")
    git(repository, "add", str(lock.relative_to(repository)))
    metadata.write_text(metadata.read_text().split("[tool.hashi.runtime]")[0])
    result = check(repository)
    assert result.returncode == 2, result.stdout + result.stderr


def test_authorized_dependency_change_still_requires_major_and_review(repository):
    _, lock = _runtime_baseline(repository)
    lock.write_text("example-package==2.0\n")
    git(repository, "add", ".")
    result = check(repository, "--cached", "--base", "HEAD",
                   "--authorized", "--major-version-change")
    assert result.returncode == 4, result.stdout + result.stderr
    assert "major-version increment" in result.stderr
    assert "independent review record" in result.stderr


@pytest.mark.parametrize("seal", ["valid", "missing", "stale"])
def test_reviewed_dependency_generation_binds_metadata_and_active_lock(repository, seal):
    metadata, lock = _runtime_baseline(repository)
    metadata.write_text(metadata.read_text().replace("4.1.2", "5.0.0a1"))
    lock.write_text("example-package==2.0\n")
    git(repository, "add", ".")
    digest = check(repository, "--cached", "--print-core-digest").stdout.strip()
    record = {
        "schema_version": 1,
        "change_id": "dependency-generation",
        "authorization_reference": "dated owner migration approval",
        "implementer": "implementer-agent",
        "reviewer": "independent-reviewer",
        "reviewed_at": "2026-11-05T16:00:00+11:00",
        "verdict": "approved",
        "product_version": "5.0.0a1",
        "core_digest": digest,
        "summary": "Reviewed the dependency generation without a Core source edit.",
    }
    if seal != "missing":
        record["reviewed_files_sha256"] = {
            str(p.relative_to(repository)).replace("\\", "/"):
            "sha256:" + hashlib.sha256(p.read_text().encode()).hexdigest()
            for p in (metadata, lock)
        }
    if seal == "stale":
        record["reviewed_files_sha256"][str(lock.relative_to(repository)).replace("\\", "/")] = "sha256:" + "0" * 64
    review = repository / "docs" / "core-reviews" / "dependency-review.json"
    review.parent.mkdir(parents=True)
    review.write_text(json.dumps(record))
    git(repository, "add", str(review.relative_to(repository)))

    # An independently reviewed candidate still needs the trusted CI label.
    unlabeled = check(repository, "--cached", "--base", "HEAD")
    assert unlabeled.returncode == 2, unlabeled.stdout + unlabeled.stderr
    result = check(repository, "--cached", "--base", "HEAD",
                   "--authorized", "--major-version-change")
    assert result.returncode == (0 if seal == "valid" else 4), result.stdout + result.stderr
    if seal != "valid":
        assert "reviewed_files_sha256" in result.stderr


def test_ordinary_metadata_patch_and_function_change_need_no_core_approval(repository):
    metadata, _ = _runtime_baseline(repository)
    metadata.write_text(metadata.read_text().replace("4.1.2", "4.1.3"))
    (repository / "feature.py").write_text("feature = True\n")
    git(repository, "add", ".")
    result = check(repository, "--cached")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "owner",
    ["../outside.lock", "/outside.lock", "\\outside.lock",
     "C:\\outside.lock", "C:outside.lock", "//server/share/outside.lock"],
)
@pytest.mark.parametrize("padding", ["", " "])
def test_runtime_lock_owner_cannot_escape_the_repository(repository, owner, padding):
    owner = padding + owner + padding
    metadata, _ = _runtime_baseline(repository)
    metadata.write_text(
        metadata.read_text().replace('"constraints/declared.lock"', json.dumps(owner))
    )
    git(repository, "add", ".")
    result = check(repository, "--cached")
    assert result.returncode == 3, result.stdout + result.stderr
    assert "inside the repository" in result.stderr


def test_whitespace_trimmed_lock_owner_retains_runtime_identity(repository):
    metadata, _ = _runtime_baseline(repository)
    metadata.write_text(
        metadata.read_text().replace(
            '"constraints/declared.lock"', '" constraints/declared.lock "'
        )
    )
    git(repository, "add", ".")
    result = check(repository, "--cached")
    assert result.returncode == 0, result.stdout + result.stderr
