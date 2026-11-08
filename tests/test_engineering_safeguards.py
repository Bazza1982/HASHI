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
    assert "independent-human or owner-approved review record" in result.stderr


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



@pytest.mark.parametrize(
    ("record_updates", "expected_code"),
    [
        ({}, 0),
        ({"owner_decision": "pending"}, 4),
        ({"owner_decision_reference": ""}, 4),
        ({"technical_review_reference": ""}, 4),
        ({"owner": ""}, 4),
        ({"core_digest": "sha256:" + "0" * 64}, 4),
    ],
)
def test_solo_owner_approved_core_change_has_real_auditable_evidence(
    repository, record_updates, expected_code
):
    (repository / "main.py").write_text("changed\n")
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "hashi-test"\nversion = "5.0.0a1"\n'
    )
    git(repository, "add", "main.py", "pyproject.toml")
    digest = check(repository, "--cached", "--print-core-digest").stdout.strip()
    record = {
        "schema_version": 1,
        "approval_mode": "owner_approved",
        "change_id": "core-v5-migration",
        "authorization_reference": "Explicit sole-owner Core v5 migration authorization",
        "implementer": "local-ai-implementer",
        "reviewer": "GPT-6 AI technical review, not a human",
        "owner": "sole-owner",
        "owner_decision": "approved",
        "owner_decision_reference": "Explicit owner approval, dated",
        "technical_review_reference": "https://example.invalid/pr/140/review",
        "reviewed_at": "2026-11-07T19:00:00+11:00",
        "verdict": "approved",
        "product_version": "5.0.0a1",
        "core_digest": digest,
        "summary": "Owner authorized after reviewing the technical evidence.",
    }
    record.update(record_updates)
    review = repository / "docs" / "core-reviews" / "solo-owner-v5.json"
    review.parent.mkdir(parents=True)
    review.write_text(json.dumps(record))
    git(repository, "add", str(review.relative_to(repository)))

    result = check(
        repository, "--cached", "--base", "HEAD",
        "--authorized", "--major-version-change"
    )
    assert result.returncode == expected_code, result.stderr
    if expected_code == 0:
        assert "authorized major-version change" in result.stdout
    else:
        assert "no added Core review record matches" in result.stderr


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


@pytest.mark.parametrize(
    ("upstream_is_main", "alter_core", "allowed"),
    [(True, False, True), (True, True, False), (False, False, False)],
)
def test_installed_hook_checks_main_merge_against_approved_upstream(
    repository, monkeypatch, upstream_is_main, alter_core, allowed
):
    import shutil

    baseline = git(repository, "rev-parse", "HEAD").stdout.strip()
    git(repository, "checkout", "-qb", "upstream")
    (repository / "main.py").write_text("approved upstream Core\n")
    git(repository, "add", "main.py")
    git(repository, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "reviewed upstream")
    upstream = git(repository, "rev-parse", "HEAD").stdout.strip()
    git(repository, "update-ref", "refs/remotes/origin/main",
        upstream if upstream_is_main else baseline)
    git(repository, "checkout", "-qb", "feature", baseline)
    (repository / "feature.py").write_text("feature\n")
    git(repository, "add", "feature.py")
    git(repository, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "feature")
    before = git(repository, "rev-parse", "HEAD").stdout.strip()
    (repository / "scripts").mkdir()
    shutil.copy2(CHECKER, repository / "scripts" / CHECKER.name)
    shutil.copytree(CHECKER.parents[1] / ".githooks", repository / ".githooks")
    git(repository, "config", "core.hooksPath", ".githooks")
    monkeypatch.setenv("HASHI_CHECK_PYTHON", sys.executable)
    monkeypatch.delenv("HASHI_CORE_EDIT_AUTHORIZED", raising=False)
    monkeypatch.delenv("HASHI_CORE_MAJOR_VERSION_CHANGE", raising=False)
    git(repository, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "merge", "--no-commit", "--no-ff", "upstream")
    if alter_core:
        (repository / "main.py").write_text("unapproved merge resolution\n")
        git(repository, "add", "main.py")
    result = subprocess.run(
        ["git", "-C", str(repository), "-c", "user.name=Test", "-c",
         "user.email=test@example.invalid", "commit", "-qm", "integrate main"],
        capture_output=True, text=True,
    )
    if allowed:
        assert result.returncode == 0, result.stderr
        assert git(repository, "show", "HEAD:main.py").stdout == "approved upstream Core\n"
        assert git(repository, "rev-parse", "HEAD^2").stdout.strip() == upstream
    else:
        assert result.returncode != 0
        assert "main.py" in result.stderr
        assert git(repository, "rev-parse", "HEAD").stdout.strip() == before
# HASHI-Fingerprint: hfp_eb03b914e0aa49ed8ff65d5466335ec0
