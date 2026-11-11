"""Synthetic admission negatives; fixture executables are never executed."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import sys

import pytest

from orchestrator.runtime_contract import (
    CORE_HOST_ROLE,
    CORE_SOURCE_PATHS,
    FUNCTIONS_ROLE,
    RuntimeContractError,
    core_source_digest,
    current_runtime_fingerprint,
    load_qualified_runtime,
    load_runtime_policy,
    locked_standard_dependencies,
)

ROOT = Path(__file__).resolve().parents[1]


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seal_table(root, except_path=None):
    return {p.relative_to(root).as_posix(): checksum(p)
            for p in root.rglob("*") if p.is_file() and p != except_path}


def write_manifest(path, value):
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return "sha256:" + checksum(path)


def installation(tmp_path, role=FUNCTIONS_ROLE):
    root, base = tmp_path / "environment", tmp_path / "cpython-base"
    root.mkdir()
    base.mkdir()
    executable = root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic admission fixture; never executable")
    (base / "python.exe").write_bytes(b"synthetic immutable CPython base")
    (root / "pyvenv.cfg").write_text(
        f"home = {base}\ninclude-system-site-packages = false\n", encoding="utf-8")
    policy = load_runtime_policy(ROOT, role=role)
    locked = locked_standard_dependencies(ROOT, policy)
    site = root / ("Lib/site-packages" if sys.platform == "win32"
                   else "lib/python3.12/site-packages")
    for name, version in locked.items():
        metadata = site / (name.replace("-", "_") + ".dist-info") / "METADATA"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(f"Name: {name}\nVersion: {version}\n", encoding="utf-8")
    packages = "\n".join(f"{name}=={version}" for name, version in sorted(locked.items()))
    runtime = replace(
        current_runtime_fingerprint(policy, code_root=ROOT),
        executable=str(executable), environment_prefix=str(root),
        dependency_digest="sha256:" + hashlib.sha256(packages.encode()).hexdigest(),
        installation_digest="",
    )
    path = root / "runtime-installation.json"
    value = {"schema_version": 1, "role": role, "runtime": runtime.to_dict(),
             "base_root": str(base), "environment_files": seal_table(root),
             "base_files": seal_table(base)}
    digest = write_manifest(path, value)
    return path, value, digest, site


@pytest.mark.parametrize("role", [CORE_HOST_ROLE, FUNCTIONS_ROLE])
def test_approved_role_installation_has_a_byte_bound_independent_identity(tmp_path, role):
    path, _, digest, _ = installation(tmp_path, role)
    qualified = load_qualified_runtime(path, expected_digest=digest, code_root=ROOT, role=role)
    assert qualified.fingerprint.role == role
    assert qualified.manifest_digest == digest
    assert qualified.runtime_id == qualified.fingerprint.runtime_id
    assert qualified.runtime_id.startswith(("cr:" if role == CORE_HOST_ROLE else "fr:") + "sha256:")
    assert qualified.executable.is_file()


@pytest.mark.parametrize("defect,expected", [
    ("missing-manifest", "unavailable"),
    ("changed-manifest", "manifest hash"),
    ("missing-digest", "trusted manifest digest"),
    ("wrong-role", "role"),
    ("wrong-prefix", "prefix"),
    ("outside-executable", "outside"),
    ("unsealed-executable", "not sealed"),
    ("changed-executable", "file hash"),
    ("changed-base", "file hash"),
    ("wrong-abi", "platform_abi"),
    ("wrong-python", "python"),
    ("wrong-policy", "runtime_policy_digest"),
    ("wrong-source", "core_source_digest"),
    ("wrong-dependency-digest", "dependency digest"),
    ("missing-package", "missing"),
    ("changed-package-version", "aiohttp"),
    ("extra-package", "dependency digest"),
    ("unlisted-file", "closure"),
    ("path-traversal", "Invalid path"),
    ("pth-injection", ".pth"),
    ("startup-injection", "startup"),
    ("duplicate-member", "duplicate"),
    ("system-site", "system site"),
    ("wrong-base-binding", "base does not match"),
    ("bool-schema", "version"),
    ("bool-protocol", "field type"),
    ("bad-file-table", "tables"),
    ("ambiguous-distribution", "ambiguous"),
])
def test_unqualified_installation_is_rejected_before_a_launch(tmp_path, defect, expected):
    path, value, digest, site = installation(tmp_path)
    runtime = value["runtime"]
    reseal = False
    if defect == "missing-manifest":
        path.unlink()
    elif defect == "changed-manifest":
        path.write_bytes(path.read_bytes() + b" ")
    elif defect == "missing-digest":
        digest = ""
    elif defect == "wrong-role":
        value["role"] = CORE_HOST_ROLE
        reseal = True
    elif defect == "wrong-prefix":
        runtime["environment_prefix"] = str(tmp_path / "wrong")
        reseal = True
    elif defect == "outside-executable":
        runtime["executable"] = str(tmp_path / "unapproved.exe")
        reseal = True
    elif defect == "unsealed-executable":
        runtime["executable"] = str(path.parent / "unapproved.exe")
        reseal = True
    elif defect == "changed-executable":
        Path(runtime["executable"]).write_bytes(b"changed")
    elif defect == "changed-base":
        (Path(value["base_root"]) / "python.exe").write_bytes(b"changed")
    elif defect == "wrong-abi":
        runtime["platform_abi"] = "unapproved-abi"
        reseal = True
    elif defect == "wrong-python":
        runtime["python"] = "3.13.0"
        runtime["python_minor"] = "3.13"
        reseal = True
    elif defect in {"wrong-policy", "wrong-source", "wrong-dependency-digest"}:
        field = {"wrong-policy": "runtime_policy_digest", "wrong-source": "core_source_digest",
                 "wrong-dependency-digest": "dependency_digest"}[defect]
        runtime[field] = "sha256:" + "f" * 64
        reseal = True
    elif defect in {"missing-package", "changed-package-version", "extra-package", "ambiguous-distribution"}:
        if defect == "missing-package":
            shutil.rmtree(site / "aiohttp.dist-info")
        elif defect == "changed-package-version":
            (site / "aiohttp.dist-info/METADATA").write_text("Name: aiohttp\nVersion: 0.0.0\n")
        else:
            metadata = site / "injected.dist-info/METADATA"
            metadata.parent.mkdir()
            name = "aiohttp" if defect == "ambiguous-distribution" else "unapproved-package"
            metadata.write_text(f"Name: {name}\nVersion: 1.0\n")
        value["environment_files"] = seal_table(path.parent, path)
        reseal = True
    elif defect == "unlisted-file":
        (path.parent / "injected.py").write_text("raise RuntimeError('never execute')\n")
    elif defect == "path-traversal":
        value["environment_files"]["../escape"] = "f" * 64
        reseal = True
    elif defect == "pth-injection":
        (path.parent / "injected.pth").write_text(str(tmp_path))
        value["environment_files"] = seal_table(path.parent, path)
        reseal = True
    elif defect == "startup-injection":
        (path.parent / "sitecustomize.py").write_text("raise RuntimeError('never execute')\n")
        value["environment_files"] = seal_table(path.parent, path)
        reseal = True
    elif defect == "duplicate-member":
        payload = path.read_text(encoding="utf-8")
        path.write_text(payload.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1', 1),
                        encoding="utf-8")
        digest = "sha256:" + checksum(path)
    elif defect in {"system-site", "wrong-base-binding"}:
        base = tmp_path / "unapproved" if defect == "wrong-base-binding" else Path(value["base_root"])
        system = "true" if defect == "system-site" else "false"
        (path.parent / "pyvenv.cfg").write_text(
            f"home = {base}\ninclude-system-site-packages = {system}\n")
        value["environment_files"] = seal_table(path.parent, path)
        reseal = True
    elif defect == "bool-schema":
        value["schema_version"] = True
        reseal = True
    elif defect == "bool-protocol":
        runtime["worker_protocol"] = True
        reseal = True
    elif defect == "bad-file-table":
        value["environment_files"] = []
        reseal = True
    else:
        raise AssertionError(defect)
    if reseal:
        digest = write_manifest(path, value)
    with pytest.raises(RuntimeContractError, match=expected):
        load_qualified_runtime(path, expected_digest=digest, code_root=ROOT)


def test_core_code_qualification_change_does_not_reversion_the_same_functions_runtime(tmp_path):
    path, value, digest, _ = installation(tmp_path)
    before = load_qualified_runtime(path, expected_digest=digest, code_root=ROOT)
    code = tmp_path / "other-core-code"
    for relative in (*CORE_SOURCE_PATHS, "pyproject.toml", "constraints/standard-py312.lock"):
        target = code / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    contract = code / "orchestrator/runtime_contract.py"
    contract.write_text(contract.read_text(encoding="utf-8") + "\n# synthetic future Core qualification\n",
                        encoding="utf-8")
    value["runtime"]["core_source_digest"] = core_source_digest(code)
    digest = write_manifest(path, value)
    after = load_qualified_runtime(path, expected_digest=digest, code_root=code)
    assert before.manifest_digest != after.manifest_digest
    assert before.fingerprint.core_source_digest != after.fingerprint.core_source_digest
    assert before.runtime_id == after.runtime_id
# HASHI-Fingerprint: hfp_9d8528f684e245e980649e4603aa2840
