"""Install bytes and their later code qualification remain separate facts."""
from __future__ import annotations

import shutil
from pathlib import Path

from orchestrator.runtime_contract import (
    CORE_SOURCE_PATHS, FUNCTIONS_ROLE, core_source_digest,
    load_qualified_runtime, load_runtime_policy, runtime_policy_digest,
)
from tests.test_runtime_installation import (
    ROOT, installation, seal_table, write_manifest,
)


def external_record(tmp_path):
    path, value, digest, site = installation(tmp_path)
    original = load_qualified_runtime(path, expected_digest=digest, code_root=ROOT)
    destination = tmp_path / "qualifications/original.json"
    destination.parent.mkdir()
    path.unlink()  # Fixture qualification metadata is moved outside sealed env bytes.
    pin = write_manifest(destination, value)
    return destination, value, pin, original, site


def copied_code(root):
    root.mkdir()
    for name in (*CORE_SOURCE_PATHS, "pyproject.toml", "constraints/standard-py312.lock",
                 "constraints/core-host-py312.lock"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    return root


def test_code_qualification_record_can_live_outside_immutable_runtime(tmp_path):
    path, _, digest, original, _ = external_record(tmp_path)
    approved = load_qualified_runtime(path, expected_digest=digest, code_root=ROOT)
    assert approved.runtime_id == original.runtime_id
    assert approved.fingerprint == original.fingerprint
    assert approved.manifest_path.parent != approved.executable.parent.parent


def test_new_core_code_qualification_does_not_renumber_unchanged_fr_bytes(tmp_path):
    path, value, _, original, _ = external_record(tmp_path)
    code = copied_code(tmp_path / "new-core")
    with (code / "__main__.py").open("a", encoding="utf-8") as stream:
        stream.write("\n# Independently qualified protected Core generation.\n")
    value["runtime"]["core_source_digest"] = core_source_digest(code)
    new_path = path.with_name("new-core.json")
    digest = write_manifest(new_path, value)
    approved = load_qualified_runtime(new_path, expected_digest=digest, code_root=code)
    assert approved.runtime_id == original.runtime_id
    assert approved.fingerprint.core_source_digest != original.fingerprint.core_source_digest
    assert approved.manifest_digest != original.manifest_digest


def test_sealed_fr_policy_remains_exact_when_checkout_lock_changes(tmp_path):
    path, value, _, _, _ = external_record(tmp_path)
    code = copied_code(tmp_path / "new-source-policy")
    environment = Path(value["runtime"]["environment_prefix"])
    profile = environment / "runtime-policy"
    (profile / "constraints").mkdir(parents=True)
    shutil.copyfile(ROOT / "pyproject.toml", profile / "pyproject.toml")
    shutil.copyfile(ROOT / "constraints/standard-py312.lock", profile / "constraints/standard-py312.lock")
    (code / "constraints/standard-py312.lock").write_text("certifi==0.0.1\n", encoding="utf-8")
    value["environment_files"] = seal_table(environment)
    digest = write_manifest(path, value)
    approved = load_qualified_runtime(path, expected_digest=digest, code_root=code)
    policy = load_runtime_policy(code, role=FUNCTIONS_ROLE, runtime_root=environment)
    assert approved.fingerprint.runtime_policy_digest == runtime_policy_digest(code, policy)
    assert approved.fingerprint.dependency_digest == value["runtime"]["dependency_digest"]
# HASHI-Fingerprint: hfp_faf292ef79bf466b8997c63367bae740
