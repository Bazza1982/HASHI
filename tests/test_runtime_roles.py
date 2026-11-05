"""Role isolation and admission are security boundaries, independent of launch policy."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import venv

import pytest

from orchestrator.runtime_contract import (
    CORE_HOST_ROLE,
    CORE_SOURCE_PATHS,
    FUNCTIONS_ROLE,
    RuntimeContractError,
    compare_runtime_boundary,
    compare_runtime_fingerprints,
    current_runtime_fingerprint,
    load_runtime_policy,
    locked_standard_dependencies,
    runtime_policy_digest,
    validate_standard_dependencies,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def clean_core_python(tmp_path_factory):
    root = tmp_path_factory.mktemp("clean-core-host")
    venv.EnvBuilder(with_pip=False, symlinks=False).create(root)
    return root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


@pytest.fixture(scope="module")
def clean_core_source(tmp_path_factory):
    root = tmp_path_factory.mktemp("sealed-core-source")
    for relative in (*CORE_SOURCE_PATHS, "pyproject.toml"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    policy = load_runtime_policy(ROOT, role=CORE_HOST_ROLE)
    lock = root / policy.standard_lock
    lock.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / policy.standard_lock, lock)
    return root


def test_clean_core_host_has_no_application_distributions(clean_core_python, clean_core_source):
    code = """
import importlib.metadata, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from orchestrator.runtime_contract import CORE_HOST_ROLE, enforce_runtime_contract
runtime = enforce_runtime_contract(Path(sys.argv[1]), role=CORE_HOST_ROLE)
assert not list(importlib.metadata.distributions())
import orchestrator.kernel_process
bad = [name for name in sys.modules if name.split('.')[0] in {'aiohttp', 'PIL', 'yaml', 'telegram', 'cryptography'}]
assert not bad, bad
print(json.dumps(runtime.to_dict()))
"""
    result = subprocess.run(
        [str(clean_core_python), "-I", "-B", "-c", code, str(clean_core_source)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    fingerprint = json.loads(result.stdout)
    assert fingerprint["role"] == CORE_HOST_ROLE
    assert Path(fingerprint["environment_prefix"]) == clean_core_python.parent.parent
    assert fingerprint["dependency_digest"] == "sha256:" + hashlib.sha256(b"").hexdigest()


def test_only_core_host_accepts_an_empty_dependency_profile():
    core = load_runtime_policy(ROOT, role=CORE_HOST_ROLE)
    functions = load_runtime_policy(ROOT, role=FUNCTIONS_ROLE)
    assert locked_standard_dependencies(ROOT, core) == {}
    validate_standard_dependencies(ROOT, core, installed={})
    with pytest.raises(RuntimeContractError, match="Core host.*third-party"):
        validate_standard_dependencies(ROOT, core, installed={"aiohttp": "3.14.3"})
    with pytest.raises(RuntimeContractError, match="missing"):
        validate_standard_dependencies(ROOT, functions, installed={})
    locked = locked_standard_dependencies(ROOT, functions)
    validate_standard_dependencies(ROOT, functions, installed=locked)
    changed = dict(locked)
    changed["aiohttp"] = "0.0.0"
    with pytest.raises(RuntimeContractError, match="aiohttp"):
        validate_standard_dependencies(ROOT, functions, installed=changed)


def test_functions_lock_cannot_be_empty_even_when_it_exists(tmp_path):
    source = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(source, encoding="utf-8")
    policy = load_runtime_policy(tmp_path, role=FUNCTIONS_ROLE)
    target = tmp_path / policy.standard_lock
    target.parent.mkdir(parents=True)
    target.write_text("# intentionally invalid empty Functions lock\n", encoding="utf-8")
    with pytest.raises(RuntimeContractError, match="empty"):
        locked_standard_dependencies(tmp_path, policy)


def test_functions_policy_change_cannot_change_core_host_policy_identity(tmp_path):
    source = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(source, encoding="utf-8")
    core = load_runtime_policy(tmp_path, role=CORE_HOST_ROLE)
    functions = load_runtime_policy(tmp_path, role=FUNCTIONS_ROLE)
    for policy in (core, functions):
        target = tmp_path / policy.standard_lock
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / policy.standard_lock).read_bytes())
    old_core = runtime_policy_digest(tmp_path, core)
    old_functions = runtime_policy_digest(tmp_path, functions)
    lock = tmp_path / functions.standard_lock
    lock.write_bytes(lock.read_bytes() + b"\n# newly qualified FR lock bytes\n")
    assert runtime_policy_digest(tmp_path, core) == old_core
    assert runtime_policy_digest(tmp_path, functions) != old_functions


def test_boundary_accepts_distinct_approved_runtime_and_remains_strict(tmp_path):
    functions = current_runtime_fingerprint(load_runtime_policy(ROOT), code_root=ROOT)
    core = replace(
        functions, role=CORE_HOST_ROLE,
        executable=str(tmp_path / "core/python"), environment_prefix=str(tmp_path / "core"),
        dependency_digest="sha256:" + "0" * 64,
        runtime_policy_digest="sha256:" + "1" * 64,
    )
    compare_runtime_boundary(core, functions, expected_functions=functions)
    assert core.runtime_id != functions.runtime_id
    with pytest.raises(RuntimeContractError, match="role"):
        compare_runtime_fingerprints(core, functions)
    changed_source = replace(functions, core_source_digest="sha256:" + "a" * 64)
    assert changed_source.runtime_id == functions.runtime_id
    with pytest.raises(RuntimeContractError, match="core_source_digest"):
        compare_runtime_boundary(core, changed_source, expected_functions=changed_source)

def test_core_python_build_update_cannot_change_the_functions_policy_identity(tmp_path):
    source = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    policy_file = tmp_path / "pyproject.toml"
    policy_file.write_text(source, encoding="utf-8")
    core = load_runtime_policy(tmp_path, role=CORE_HOST_ROLE)
    functions = load_runtime_policy(tmp_path, role=FUNCTIONS_ROLE)
    for policy in (core, functions):
        lock = tmp_path / policy.standard_lock
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_bytes((ROOT / policy.standard_lock).read_bytes())
    old_core = runtime_policy_digest(tmp_path, core)
    old_functions = runtime_policy_digest(tmp_path, functions)
    source = source.replace('python = "3.12.13"', 'python = "3.12.14"', 1)
    source = source.replace('portable-build-date = "20260303"', 'portable-build-date = "20260401"', 1)
    policy_file.write_text(source, encoding="utf-8")
    core = load_runtime_policy(tmp_path, role=CORE_HOST_ROLE)
    functions = load_runtime_policy(tmp_path, role=FUNCTIONS_ROLE)
    assert core.python_text == "3.12.14"
    assert core.portable_build_date == "20260401"
    assert functions.python_text == "3.12.13"
    assert functions.portable_build_date == "20260303"
    assert runtime_policy_digest(tmp_path, core) != old_core
    assert runtime_policy_digest(tmp_path, functions) == old_functions


@pytest.mark.parametrize("field,value", [
    ("role", CORE_HOST_ROLE),
    ("executable", "unapproved/python"),
    ("environment_prefix", "unapproved/environment"),
    ("dependency_digest", "sha256:" + "b" * 64),
    ("runtime_policy_digest", "sha256:" + "c" * 64),
    ("worker_protocol", 999),
    ("generation_schema", 999),
    ("core_api", 999),
    ("function_api", 999),
    ("platform", "unapproved-os"),
    ("machine", "unapproved-architecture"),
])
def test_boundary_rejects_any_unqualified_function_identity(field, value):
    functions = current_runtime_fingerprint(load_runtime_policy(ROOT), code_root=ROOT)
    core = replace(functions, role=CORE_HOST_ROLE)
    bad = replace(functions, **{field: value})
    with pytest.raises(RuntimeContractError):
        compare_runtime_boundary(core, bad, expected_functions=functions)
# HASHI-Fingerprint: hfp_5c4a9a9d63f84792bec9cde06f9c665b
