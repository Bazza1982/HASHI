"""Core approval follows code/wire contracts, not independent runtime dependencies."""
from __future__ import annotations

import hashlib
import json

import pytest

from tests.test_engineering_safeguards import check, git, repository as repository


def _runtime_baseline(root):
    metadata = root / "pyproject.toml"
    metadata.write_text(
        metadata.read_text()
        + '\n[tool.hashi.runtime]\n'
        + 'standard-lock = "constraints/declared.lock"\n'
        + 'core-host-lock = "constraints/core.lock"\n'
        + 'python = "3.12.13"\nfunctions-python = "3.12.13"\n'
        + 'core-api = 1\nfunction-api = 1\nworker-model = "process"\n'
        + 'worker-protocol = 1\ngeneration-schema = 1\n'
    )
    folder = root / "constraints"
    folder.mkdir()
    lock = folder / "declared.lock"
    lock.write_text("example-package==1.0\n")
    (folder / "core.lock").write_text("# zero application packages\n")
    git(root, "add", ".")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "role baseline")
    return metadata, lock


@pytest.mark.parametrize(
    "change",
    ["functions_pin", "functions_comment", "functions_owner",
     "functions_python", "core_python", "core_lock", "metadata_patch"],
)
def test_independent_runtime_revision_does_not_force_core_upgrade(repository, change):
    metadata, lock = _runtime_baseline(repository)
    if change == "functions_pin":
        lock.write_text("example-package==2.0\n")
    elif change == "functions_comment":
        lock.write_text(lock.read_text() + "# independently qualified revision\n")
    elif change == "functions_owner":
        replacement = lock.with_name("replacement.lock")
        replacement.write_text(lock.read_text())
        metadata.write_text(metadata.read_text().replace("constraints/declared.lock",
                                                         "constraints/replacement.lock"))
    elif change == "core_lock":
        (lock.parent / "core.lock").write_text("# new empty qualified Core runtime\n")
    else:
        old, new = {
            "functions_python": ('functions-python = "3.12.13"', 'functions-python = "3.12.14"'),
            "core_python": ('\npython = "3.12.13"', '\npython = "3.12.14"'),
            "metadata_patch": ("4.1.2", "4.1.3"),
        }[change]
        metadata.write_text(metadata.read_text().replace(old, new))
    git(repository, "add", ".")
    result = check(repository, "--cached", "--base", "HEAD")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "authorized" not in result.stdout


@pytest.mark.parametrize(
    ("field", "original", "candidate"),
    [("core-api", "1", "2"), ("function-api", "1", "2"),
     ("worker-protocol", "1", "2"), ("generation-schema", "1", "2"),
     ("worker-model", '"process"', '"another-model"'),
     ("core-api", "1", "true")],
)
def test_core_wire_contract_change_is_not_hidden_by_unchanged_source(
    repository, field, original, candidate
):
    metadata, _ = _runtime_baseline(repository)
    metadata.write_text(metadata.read_text().replace(f"{field} = {original}",
                                                     f"{field} = {candidate}"))
    git(repository, "add", "pyproject.toml")
    result = check(repository, "--cached", "--base", "HEAD")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "pyproject.toml" in result.stderr


def test_staged_core_contract_change_cannot_be_hidden_in_worktree(repository):
    metadata, _ = _runtime_baseline(repository)
    original = metadata.read_text()
    metadata.write_text(original.replace("worker-protocol = 1", "worker-protocol = 2"))
    git(repository, "add", "pyproject.toml")
    metadata.write_text(original)
    assert check(repository).returncode == 2
    assert check(repository, "--cached").returncode == 2


@pytest.mark.parametrize("seal", ["valid", "missing", "stale"])
def test_solo_owner_can_approve_exact_core_contract_without_sealing_functions_lock(
    repository, seal
):
    metadata, lock = _runtime_baseline(repository)
    metadata.write_text(metadata.read_text().replace("4.1.2", "5.0.0a1")
                        .replace("worker-protocol = 1", "worker-protocol = 2"))
    git(repository, "add", "pyproject.toml")
    digest = check(repository, "--cached", "--print-core-digest").stdout.strip()
    record = {
        "schema_version": 1, "approval_mode": "owner_approved",
        "change_id": "core-wire-change",
        "authorization_reference": "Dated explicit owner migration decision",
        "implementer": "implementation-agent",
        "reviewer": "AI technical reviewer, not a second human",
        "owner": "sole-repository-owner", "owner_decision": "approved",
        "owner_decision_reference": "Dated explicit owner decision",
        "technical_review_reference": "Recorded technical review and regressions",
        "reviewed_at": "2026-11-08T16:30:00+11:00", "verdict": "approved",
        "product_version": "5.0.0a1", "core_digest": digest,
        "summary": "Owner-approved Core wire contract; Functions lock has its own lifecycle.",
    }
    if seal != "missing":
        record["reviewed_files_sha256"] = {
            "pyproject.toml": "sha256:" + (
                hashlib.sha256(metadata.read_text().encode()).hexdigest()
                if seal == "valid" else "0" * 64
            )
        }
    review = repository / "docs/core-reviews/wire-review.json"
    review.parent.mkdir(parents=True)
    review.write_text(json.dumps(record))
    git(repository, "add", "docs/core-reviews")
    # A separate Functions lock revision does not change Core review scope.
    lock.write_text("example-package==2.0\n")
    git(repository, "add", str(lock.relative_to(repository)))
    assert check(repository, "--cached", "--base", "HEAD").returncode == 2
    result = check(repository, "--cached", "--base", "HEAD",
                   "--authorized", "--major-version-change")
    assert result.returncode == (0 if seal == "valid" else 4), result.stdout + result.stderr
    if seal != "valid":
        assert "reviewed_files_sha256" in result.stderr


@pytest.mark.parametrize(
    "owner",
    ["../outside.lock", "/outside.lock", "\\outside.lock", "C:\\outside.lock",
     "C:outside.lock", "//server/share/outside.lock"],
)
@pytest.mark.parametrize("padding", ["", " "])
def test_runtime_lock_owner_cannot_escape_the_repository(repository, owner, padding):
    metadata, _ = _runtime_baseline(repository)
    metadata.write_text(metadata.read_text().replace(
        '"constraints/declared.lock"', json.dumps(padding + owner + padding)))
    git(repository, "add", "pyproject.toml")
    result = check(repository, "--cached")
    assert result.returncode == 3, result.stdout + result.stderr
    assert "inside the repository" in result.stderr


def test_trimmed_lock_path_retains_identity_without_core_approval(repository):
    metadata, _ = _runtime_baseline(repository)
    metadata.write_text(metadata.read_text().replace(
        '"constraints/declared.lock"', '" constraints/declared.lock "'))
    git(repository, "add", "pyproject.toml")
    result = check(repository, "--cached")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("field", ["standard-lock", "core-host-lock"])
@pytest.mark.parametrize(
    "relative", ["constraints/nested/profile.lock", r"constraints\nested\profile.lock",
                 r"constraints\nested/profile.lock"],
)
def test_relative_lock_revision_needs_no_core_approval(repository, field, relative):
    from pathlib import PureWindowsPath

    metadata, standard = _runtime_baseline(repository)
    previous = standard if field == "standard-lock" else standard.parent / "core.lock"
    original = previous.relative_to(repository).as_posix()
    metadata.write_text(metadata.read_text().replace(json.dumps(original), json.dumps(relative)))
    target = repository.joinpath(*PureWindowsPath(relative).parts)
    target.parent.mkdir(parents=True)
    target.write_text(previous.read_text())
    git(repository, "add", ".")

    result = check(repository, "--cached", "--base", "HEAD")

    assert result.returncode == 0, result.stdout + result.stderr
# HASHI-Fingerprint: hfp_c06d07c478e74e33a0916e01bba959f9
