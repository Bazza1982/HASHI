from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from orchestrator import function_source_publication as publication
from orchestrator.function_generation import (
    FunctionGenerationError, SourceEntry, build_source_manifest_from_entries,
    verify_manifest_source_commit,
)
from orchestrator.runtime_contract import CORE_SOURCE_PATHS, FUNCTIONS_ROLE, RuntimeContractError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def exported(tmp_path, monkeypatch):
    git = tmp_path / "source"
    for relative in CORE_SOURCE_PATHS:
        target = git / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    fixture = git / "orchestrator/publication_fixture.py"
    fixture.write_text("VALUE = 'committed fixture'\n", encoding="utf-8")
    for args in (("init", "-q"), ("add", "."), (
            "-c", "user.name=Qualification Fixture",
            "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")):
        subprocess.run(["git", "-C", str(git), *args], check=True, capture_output=True)
    snapshot = publication.capture_git_source(git)
    package = tmp_path / "package"
    shutil.copytree(git, package, ignore=shutil.ignore_patterns(".git"))
    runtime_parent = tmp_path / "runtimes"
    runtime_parent.mkdir()
    publication.publish_source(package, runtime_parent, snapshot)
    bound = SimpleNamespace(fingerprint=SimpleNamespace(
        role=FUNCTIONS_ROLE, environment_prefix=str(runtime_parent / "functions")))
    monkeypatch.setattr(publication, "current_process_runtime", lambda: bound)
    manifest = build_source_manifest_from_entries([SourceEntry(
        module="orchestrator.publication_fixture",
        relative_path="orchestrator/publication_fixture.py",
        sha256=hashlib.sha256((package / "orchestrator/publication_fixture.py").read_bytes()).hexdigest())])
    return git, package, runtime_parent, snapshot, manifest


def test_gitless_source_accepts_only_the_installer_published_exact_bytes(exported):
    _, package, _, snapshot, manifest = exported
    assert verify_manifest_source_commit(manifest, code_root=package) == snapshot["source_commit"]


def test_plain_self_asserted_package_snapshot_cannot_qualify_without_admission(exported, monkeypatch):
    _, package, _, snapshot, manifest = exported
    (package / publication.SNAPSHOT_NAME).write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(publication, "current_process_runtime", lambda: None)
    with pytest.raises(FunctionGenerationError, match="privately admitted"):
        verify_manifest_source_commit(manifest, code_root=package)


def test_recomputed_candidate_digest_cannot_publish_changed_source(exported):
    _, package, _, _, manifest = exported
    path = package / manifest.entries[0].relative_path
    path.write_text("VALUE = 'not published'\n", encoding="utf-8")
    changed = build_source_manifest_from_entries([replace(
        manifest.entries[0], sha256=hashlib.sha256(path.read_bytes()).hexdigest())])
    with pytest.raises(FunctionGenerationError, match="source bytes"):
        verify_manifest_source_commit(changed, code_root=package)


def test_unpublished_source_member_is_denied(exported):
    _, package, _, _, manifest = exported
    path = package / "orchestrator/unpublished.py"
    path.write_text("VALUE = 2\n", encoding="utf-8")
    changed = build_source_manifest_from_entries([
        *manifest.entries, SourceEntry("orchestrator.unpublished",
                                      "orchestrator/unpublished.py",
                                      hashlib.sha256(path.read_bytes()).hexdigest())])
    with pytest.raises(FunctionGenerationError, match="source bytes"):
        verify_manifest_source_commit(changed, code_root=package)


def test_source_publication_is_bound_to_the_exact_installed_program(exported):
    _, package, parent, _, manifest = exported
    path = parent / publication.PUBLICATION_NAME
    value = json.loads(path.read_bytes())
    value["code_root"] = str(package.parent / "other")
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(FunctionGenerationError, match="authority/source root"):
        verify_manifest_source_commit(manifest, code_root=package)


def test_existing_git_checkout_never_falls_back_to_a_publication(exported):
    git, _, _, snapshot, manifest = exported
    assert verify_manifest_source_commit(manifest, code_root=git) == snapshot["source_commit"]
    path = git / manifest.entries[0].relative_path
    path.write_text("VALUE = 'dirty git'\n", encoding="utf-8")
    changed = build_source_manifest_from_entries([replace(
        manifest.entries[0], sha256=hashlib.sha256(path.read_bytes()).hexdigest())])
    with pytest.raises(FunctionGenerationError, match="not committed"):
        verify_manifest_source_commit(changed, code_root=git)
    with pytest.raises(RuntimeContractError, match="modified tracked"):
        publication.capture_git_source(git)


def test_publication_rejects_redirects_duplicates_and_escape_paths(exported):
    _, package, parent, snapshot, manifest = exported
    invalid = {**snapshot, "files": {**snapshot["files"], "../escape": {
        "sha256": "0" * 64, "executable": False}}}
    with pytest.raises(RuntimeContractError, match="Unsafe"):
        publication.validate_snapshot(invalid, package)
    path = parent / publication.PUBLICATION_NAME
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(FunctionGenerationError, match="Duplicate"):
        verify_manifest_source_commit(manifest, code_root=package)
