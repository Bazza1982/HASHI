from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

from orchestrator.runtime_contract import (
    CORE_SOURCE_PATHS,
    RuntimeContractError,
    compare_runtime_fingerprints,
    current_runtime_fingerprint,
    load_runtime_policy,
    runtime_policy_digest,
    validate_standard_dependencies,
    validate_runtime_policy,
)
from orchestrator.function_contract import PROCESS_IDENTITY_MODULES
from orchestrator.function_generation import (
    FUNCTION_GENERATION_ENTRYPOINTS,
    FUNCTION_GENERATION_SCHEMA_VERSION,
)
from orchestrator.function_worker_protocol import FUNCTION_WORKER_PROTOCOL_VERSION
from orchestrator.manager_registry import FUNCTION_MANAGER_SPECS

ROOT = Path(__file__).resolve().parents[1]


def _write_policy(
    tmp_path: Path,
    *,
    implementation="cpython",
    python_version="3.12.13",
):
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[tool.hashi.runtime]",
                f'implementation = "{implementation}"',
                f'python = "{python_version}"',
                'standard-lock = "constraints/standard-py312.lock"',
                'portable-build-date = "20260303"',
                "core-api = 4",
                "function-api = 7",
                'worker-model = "per-agent-process"',
                "worker-protocol = 3",
                "generation-schema = 5",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_runtime_policy_has_one_exact_python_minor(tmp_path):
    _write_policy(tmp_path)

    policy = load_runtime_policy(tmp_path)

    assert policy.implementation == "cpython"
    assert policy.python_text == "3.12.13"
    assert policy.python_minor == (3, 12)
    assert policy.requires_python == ">=3.12,<3.13"
    assert policy.standard_lock == "constraints/standard-py312.lock"
    assert policy.portable_build_date == "20260303"
    assert policy.core_api == 4
    assert policy.function_api == 7
    assert policy.worker_model == "per-agent-process"
    assert policy.worker_protocol == 3
    assert policy.generation_schema == 5


def test_current_process_satisfies_repository_runtime_contract():
    policy = load_runtime_policy(ROOT)
    fingerprint = current_runtime_fingerprint(policy, code_root=ROOT)

    validate_runtime_policy(policy, fingerprint)
    validate_standard_dependencies(ROOT, policy)

    assert fingerprint.implementation == "cpython"
    assert fingerprint.python_minor == "3.12"
    assert fingerprint.cache_tag == "cpython-312"
    assert fingerprint.platform_abi
    if sys.platform == "win32":
        assert fingerprint.platform_abi == ".cp312-win_amd64.pyd"
    else:
        assert fingerprint.platform_abi.startswith("cpython-312-")
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint.dependency_digest)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint.runtime_policy_digest)
    assert fingerprint.worker_model == "per-agent-process"
    assert fingerprint.worker_protocol == 1
    assert fingerprint.generation_schema == 2


def test_machine_policy_matches_implemented_worker_protocols():
    policy = load_runtime_policy(ROOT)

    assert policy.worker_model == "per-agent-process"
    assert policy.worker_protocol == FUNCTION_WORKER_PROTOCOL_VERSION
    assert policy.generation_schema == FUNCTION_GENERATION_SCHEMA_VERSION


def test_function_managers_and_entrypoints_are_disjoint_from_core():
    core_modules = {
        relative.removesuffix("/__init__.py").removesuffix(".py").replace("/", ".")
        for relative in CORE_SOURCE_PATHS
    }

    assert {spec.module for spec in FUNCTION_MANAGER_SPECS}.isdisjoint(core_modules)
    assert set(FUNCTION_GENERATION_ENTRYPOINTS).isdisjoint(core_modules)


def test_wrong_python_minor_is_rejected_as_core_migration_not_function_reboot():
    policy = load_runtime_policy(ROOT)
    current = current_runtime_fingerprint(policy, code_root=ROOT)
    incompatible = dataclasses.replace(current, python="3.13.2", python_minor="3.13")

    with pytest.raises(RuntimeContractError, match="function /reboot cannot change"):
        validate_runtime_policy(policy, incompatible)


def test_unapproved_python_patch_is_rejected_as_planned_core_migration():
    policy = load_runtime_policy(ROOT)
    current = current_runtime_fingerprint(policy, code_root=ROOT)
    incompatible = dataclasses.replace(current, python="3.12.12")

    with pytest.raises(RuntimeContractError, match="approved production patch"):
        validate_runtime_policy(policy, incompatible)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("core_api", 99),
        ("function_api", 99),
        ("worker_model", "in-process"),
        ("worker_protocol", 99),
        ("generation_schema", 99),
    ],
)
def test_process_contract_rejects_noncanonical_worker_boundary(field, replacement):
    policy = load_runtime_policy(ROOT)
    current = current_runtime_fingerprint(policy, code_root=ROOT)
    incompatible = dataclasses.replace(current, **{field: replacement})

    with pytest.raises(RuntimeContractError, match=field):
        validate_runtime_policy(policy, incompatible)


def test_standard_dependency_generation_rejects_missing_or_drifted_package():
    policy = load_runtime_policy(ROOT)
    installed = {
        re.split(r"[=<>!~ ;\[]", line, maxsplit=1)[0].strip().lower(): line.split(
            "==", 1
        )[1]
        .split(";", 1)[0]
        .strip()
        for line in (ROOT / policy.standard_lock)
        .read_text(encoding="utf-8")
        .splitlines()
        if line and not line.startswith(("#", " "))
    }
    installed.pop("aiohttp")
    installed["httpx"] = "0.0.0"

    with pytest.raises(RuntimeContractError) as raised:
        validate_standard_dependencies(ROOT, policy, installed=installed)

    assert "aiohttp: installed=missing" in str(raised.value)
    assert "httpx: installed=0.0.0" in str(raised.value)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("platform_abi", "cpython-312-arm64-linux-gnu"),
        ("machine", "arm64"),
        ("environment_prefix", "/another/core/environment"),
        ("runtime_policy_digest", "sha256:" + "f" * 64),
        ("dependency_digest", "sha256:" + "0" * 64),
        ("core_source_digest", "sha256:" + "1" * 64),
        ("core_api", 99),
        ("function_api", 99),
        ("worker_model", "in-process"),
        ("worker_protocol", 99),
        ("generation_schema", 99),
    ],
)
def test_candidate_must_match_running_core_fingerprint(field, replacement):
    policy = load_runtime_policy(ROOT)
    core = current_runtime_fingerprint(policy, code_root=ROOT)
    candidate = dataclasses.replace(core, **{field: replacement})

    with pytest.raises(RuntimeContractError, match=field):
        compare_runtime_fingerprints(core, candidate)


def test_packaging_metadata_is_derived_from_runtime_policy():
    policy = load_runtime_policy(ROOT)
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert f'requires-python = "{policy.requires_python}"' in pyproject
    assert f"Programming Language :: Python :: {policy.python_minor_text}" in pyproject
    assert "Programming Language :: Python :: 3.10" not in pyproject
    assert "Programming Language :: Python :: 3.11" not in pyproject
    assert "Programming Language :: Python :: 3.13" not in pyproject


def test_runtime_policy_digest_binds_policy_and_exact_lock_bytes(tmp_path):
    _write_policy(tmp_path)
    lock = tmp_path / "constraints" / "standard-py312.lock"
    lock.parent.mkdir()
    lock.write_text("example==1.0\n", encoding="utf-8")
    policy = load_runtime_policy(tmp_path)

    first = runtime_policy_digest(tmp_path, policy)
    lock.write_text("example==1.1\n", encoding="utf-8")
    second = runtime_policy_digest(tmp_path, policy)

    assert re.fullmatch(r"sha256:[0-9a-f]{64}", first)
    assert first != second


def test_deployment_versions_derive_from_the_runtime_authority():
    policy = load_runtime_policy(ROOT)
    expected_python = policy.python_text
    expected_date = policy.portable_build_date

    assert f"FROM python:{expected_python}-slim" in (
        ROOT / "Dockerfile.enterprise"
    ).read_text(encoding="utf-8")
    mac_builder = (ROOT / "mac" / "prepare_usb.sh").read_text(encoding="utf-8")
    assert expected_python in mac_builder
    assert expected_date in mac_builder

    windows_builder = (ROOT / "packaging" / "portable_windows" / "build.py").read_text(
        encoding="utf-8"
    )
    assert "load_runtime_policy(HASHI_ROOT)" in windows_builder
    assert "validate_portable_dependency_generation()" in windows_builder
    assert "import tomllib" not in windows_builder
    assert "PYTHON_VERSION = _RUNTIME_POLICY.python_text" in windows_builder
    assert "PYTHON_BUILD_DATE = _RUNTIME_POLICY.portable_build_date" in windows_builder


def test_standard_lock_covers_every_standard_requirement_and_launch_path():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    lock = (ROOT / "constraints" / "standard-py312.lock").read_text(encoding="utf-8")
    locked_names = {
        re.split(r"[=<>!~ ;\[]", line, maxsplit=1)[0].strip().lower()
        for line in lock.splitlines()
        if line and not line.startswith(("#", " "))
    }
    required_names = {
        re.split(r"[=<>!~ ;\[]", line, maxsplit=1)[0].strip().lower()
        for line in requirements.splitlines()
        if line and not line.startswith(("#", " "))
    }

    assert required_names <= locked_names
    for relative in (
        "bin/bridge-u.sh",
        "bin/bridge-u.bat",
        "Dockerfile.enterprise",
        "mac/prepare_usb.sh",
        "postinstall.js",
    ):
        content = (ROOT / relative).read_text(encoding="utf-8")
        assert "standard-py312.lock" in content


def test_windows_timezone_database_is_declared_and_hash_locked():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    standard_lock = (ROOT / "constraints" / "standard-py312.lock").read_text(
        encoding="utf-8"
    )
    portable_lock = (
        ROOT / "packaging" / "portable_windows" / "requirements.lock"
    ).read_text(encoding="utf-8")

    assert 'tzdata>=2026.3; platform_system == "Windows"' in requirements
    assert "tzdata>=2026.3; platform_system == 'Windows'" in project
    assert "tzdata==2026.3 ; sys_platform == 'win32'" in standard_lock
    tzdata_block = portable_lock.split("tzdata==2026.3", 1)[1].split(
        "\nuvicorn==", 1
    )[0]
    assert tzdata_block.count("--hash=sha256:") >= 2


def test_all_first_party_launchers_run_the_runtime_contract_checker():
    for relative in (
        "bin/bridge-u.sh",
        "bin/bridge-u.bat",
        "cli.js",
        "onboard-cli.js",
        "postinstall.js",
        "mac/start_main.command",
        "mac/start_tui.command",
        "mac/tui_onboarding.command",
        "windows/start_tui.bat",
        "windows/TUI_onboarding.bat",
    ):
        content = (ROOT / relative).read_text(encoding="utf-8")
        if relative == "onboard-cli.js":
            assert "require('./cli')" in content
            continue
        assert "check_runtime_contract.py" in content


def test_npm_package_contains_runtime_authority_and_reproducible_lock():
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    included = set(package["files"])

    assert {
        "adapters/",
        "browser_gateway/",
        "flow/",
        "locales/",
        "nagare/",
        "orchestrator/",
        "pyproject.toml",
        "remote/",
        "constraints/standard-py312.lock",
        "scripts/check_runtime_contract.py",
        "scripts/launcher_helper.py",
        "scripts/resolve_instance_runtime.py",
        "tools/",
        "transports/",
        "tui/",
    } <= included
    assert {
        "!flow/runs/**",
        "!flow/evaluation_kb/improvements/**",
        "!flow/evaluation_kb/workflow_scores/**",
        "!flow/evaluation_kb/workflow_versions/**",
        "!superloops/loops/**",
        "!superloops/recordings/**",
    } <= included


def test_vendored_usecomputer_binary_has_reviewed_provenance():
    binary = ROOT / "tools" / "bin" / "usecomputer"
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    notice = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

    assert digest == "6ed5444144e08de1225a64f3e990115748fb854eece7aaffc0469c5898d438e4"
    assert "usecomputer 0.1.11" in notice
    assert digest in notice
    assert "Copyright (c) 2026 Tommy D. Rossi" in notice


def test_retired_windows_usb_packagers_are_not_parallel_release_paths():
    assert not (ROOT / "windows" / "prepare_usb.bat").exists()
    assert not (ROOT / "windows" / "prepare_usb_international.bat").exists()
    assert not (ROOT / "fix_usb_path.bat").exists()

    builder = ROOT / "packaging" / "portable_windows" / "build.py"
    guide = ROOT / "packaging" / "portable_windows" / "README.md"
    assert builder.is_file()
    assert guide.is_file()


def test_ci_runtime_matrix_uses_only_the_canonical_minor():
    policy = load_runtime_policy(ROOT)
    workflows = ROOT / ".github" / "workflows"
    for path in workflows.glob("*.yml"):
        content = path.read_text(encoding="utf-8")
        declared = re.findall(
            r'python-version:\s*["\[]?([0-9]+\.[0-9]+(?:\.[0-9]+)?)',
            content,
        )
        assert set(declared) <= {policy.python_text}, path.name


def test_main_enforces_core_runtime_before_other_project_imports():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    guard_index = next(
        index
        for index, node in enumerate(tree.body)
        if isinstance(node, ast.Try)
        and any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "enforce_runtime_contract"
            for child in ast.walk(node)
        )
    )
    first_function_import = next(
        index
        for index, node in enumerate(tree.body)
        if isinstance(node, ast.ImportFrom)
        and node.module == "orchestrator.instance_lock"
    )

    assert guard_index < first_function_import


def test_core_has_no_static_binding_to_replaceable_function_modules():
    project_roots = {
        "adapters",
        "flow",
        "nagare",
        "orchestrator",
        "remote",
        "tools",
        "transports",
    }
    core_modules = {
        relative.removesuffix("/__init__.py").removesuffix(".py").replace("/", ".")
        for relative in CORE_SOURCE_PATHS
    }
    violations = []

    for relative in CORE_SOURCE_PATHS:
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        for node in tree.body:
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
                if node.module in project_roots:
                    imported.extend(
                        f"{node.module}.{alias.name}"
                        for alias in node.names
                        if alias.name != "*"
                        and (ROOT / node.module / f"{alias.name}.py").is_file()
                    )
            for dependency in imported:
                if dependency.split(".", 1)[0] not in project_roots:
                    continue
                if dependency in project_roots or dependency in core_modules:
                    continue
                violations.append(f"{relative} -> {dependency}")

    assert violations == []


def test_process_identity_manifest_matches_importable_core_modules():
    importable_core = {
        relative.removesuffix("/__init__.py").removesuffix(".py").replace("/", ".")
        for relative in CORE_SOURCE_PATHS
        if relative.startswith(("adapters/", "orchestrator/", "remote/", "tools/"))
    }

    assert PROCESS_IDENTITY_MODULES == importable_core


def test_instance_lock_remains_in_core_and_local_locks_belong_to_function_processes():
    assert "orchestrator/instance_lock.py" in CORE_SOURCE_PATHS
    from orchestrator.function_contract import is_function_module_name

    assert is_function_module_name("orchestrator.process_resources")


def test_cross_platform_modules_do_not_call_posix_only_fchmod_directly():
    violations = []
    for top_level in (
        "adapters",
        "flow",
        "nagare",
        "orchestrator",
        "remote",
        "tools",
        "transports",
    ):
        for path in (ROOT / top_level).rglob("*.py"):
            if path.name == "file_permissions.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "os"
                    and node.func.attr == "fchmod"
                ):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert violations == []
