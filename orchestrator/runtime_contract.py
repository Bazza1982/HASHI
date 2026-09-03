"""Authoritative HASHI Core runtime compatibility contract.

This module is deliberately standard-library only.  It is imported before any
function-layer module so an unsupported interpreter cannot partially construct
the HASHI process.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import re
import struct
import sys
import sysconfig
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class RuntimeContractError(RuntimeError):
    """The process or candidate generation violates the Core runtime contract."""


CORE_SOURCE_PATHS = (
    "__main__.py",
    "main.py",
    "adapters/stream_events.py",
    "orchestrator/activity_digest.py",
    "orchestrator/bootstrap_logging.py",
    "orchestrator/config.py",
    "orchestrator/enterprise/profile.py",
    "orchestrator/function_generation.py",
    "orchestrator/hot_reload.py",
    "orchestrator/instance_lock.py",
    "orchestrator/lifecycle_state.py",
    "orchestrator/manager_registry.py",
    "orchestrator/onboarding_gate.py",
    "orchestrator/pathing.py",
    "orchestrator/process_resources.py",
    "orchestrator/reboot_manager.py",
    "orchestrator/runtime_contract.py",
    "orchestrator/runtime_defaults.py",
    "orchestrator/pcm.py",
    "orchestrator/shutdown_manager.py",
    "orchestrator/startup_manager.py",
    "orchestrator/terminal_console.py",
    "orchestrator/ui_language.py",
)


@dataclass(frozen=True)
class RuntimePolicy:
    implementation: str
    python_version: tuple[int, int, int]
    standard_lock: str
    portable_build_date: str
    core_api: int
    function_api: int

    @property
    def python_text(self) -> str:
        return ".".join(str(part) for part in self.python_version)

    @property
    def python_minor(self) -> tuple[int, int]:
        return self.python_version[:2]

    @property
    def python_minor_text(self) -> str:
        return f"{self.python_minor[0]}.{self.python_minor[1]}"

    @property
    def requires_python(self) -> str:
        major, minor = self.python_minor
        return f">={major}.{minor},<{major}.{minor + 1}"


@dataclass(frozen=True)
class RuntimeFingerprint:
    implementation: str
    python: str
    python_minor: str
    cache_tag: str
    platform_abi: str
    platform: str
    machine: str
    pointer_bits: int
    executable: str
    environment_prefix: str
    dependency_digest: str
    core_source_digest: str
    core_api: int
    function_api: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RuntimeFingerprint:
        return cls(
            implementation=str(value["implementation"]),
            python=str(value["python"]),
            python_minor=str(value["python_minor"]),
            cache_tag=str(value["cache_tag"]),
            platform_abi=str(value["platform_abi"]),
            platform=str(value["platform"]),
            machine=str(value["machine"]),
            pointer_bits=int(value["pointer_bits"]),
            executable=str(value["executable"]),
            environment_prefix=str(value["environment_prefix"]),
            dependency_digest=str(value["dependency_digest"]),
            core_source_digest=str(value["core_source_digest"]),
            core_api=int(value["core_api"]),
            function_api=int(value["function_api"]),
        )

    @property
    def runtime_id(self) -> str:
        return (
            f"{self.implementation}-{self.python_minor}/core-{self.core_api}/"
            f"function-{self.function_api}/{self.cache_tag}/{self.machine}"
        )


_RUNTIME_SECTION = re.compile(
    r"(?ms)^\[tool\.hashi\.runtime\]\s*$\n(?P<body>.*?)(?=^\[|\Z)"
)
_STRING_SETTING = re.compile(r'(?m)^\s*([a-z][a-z0-9_-]*)\s*=\s*"([^"]+)"\s*$')
_INTEGER_SETTING = re.compile(r"(?m)^\s*([a-z][a-z0-9_-]*)\s*=\s*([0-9]+)\s*$")


def load_runtime_policy(code_root: Path) -> RuntimePolicy:
    """Read the single machine-readable policy from ``pyproject.toml``.

    A tiny parser is used intentionally: an old/unsupported Python must still
    be able to print the required version instead of failing first because
    ``tomllib`` is unavailable.
    """

    policy_path = Path(code_root).resolve() / "pyproject.toml"
    try:
        raw = policy_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeContractError(
            f"HASHI runtime policy is unavailable: {policy_path}: {exc}"
        ) from exc
    match = _RUNTIME_SECTION.search(raw)
    if match is None:
        raise RuntimeContractError(
            "pyproject.toml does not define [tool.hashi.runtime]"
        )
    body = match.group("body")
    values: dict[str, str] = dict(_STRING_SETTING.findall(body))
    values.update(dict(_INTEGER_SETTING.findall(body)))
    required = {
        "implementation",
        "python",
        "standard-lock",
        "portable-build-date",
        "core-api",
        "function-api",
    }
    missing = sorted(required - values.keys())
    if missing:
        raise RuntimeContractError(
            f"HASHI runtime policy is incomplete; missing={missing}"
        )
    version_match = re.fullmatch(
        r"([0-9]+)\.([0-9]+)\.([0-9]+)", values["python"]
    )
    if version_match is None:
        raise RuntimeContractError(
            "tool.hashi.runtime.python must be an exact major.minor.patch value"
        )
    standard_lock = values["standard-lock"].strip()
    lock_path = Path(standard_lock)
    if not standard_lock or lock_path.is_absolute() or ".." in lock_path.parts:
        raise RuntimeContractError(
            "tool.hashi.runtime.standard-lock must be a repository-relative path"
        )
    portable_build_date = values["portable-build-date"].strip()
    if re.fullmatch(r"[0-9]{8}", portable_build_date) is None:
        raise RuntimeContractError(
            "tool.hashi.runtime.portable-build-date must be YYYYMMDD"
        )
    return RuntimePolicy(
        implementation=values["implementation"].strip().lower(),
        python_version=tuple(int(part) for part in version_match.groups()),
        standard_lock=standard_lock,
        portable_build_date=portable_build_date,
        core_api=int(values["core-api"]),
        function_api=int(values["function-api"]),
    )


def _installed_distributions() -> dict[str, str]:
    installed: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = str(distribution.metadata.get("Name") or "").strip().lower()
        if not name:
            continue
        normalized = re.sub(r"[-_.]+", "-", name)
        installed[normalized] = str(distribution.version)
    return installed


def dependency_digest() -> str:
    """Hash the effective installed distribution set for ABI drift detection."""

    installed = _installed_distributions()
    payload = "\n".join(
        f"{name}=={version}" for name, version in sorted(installed.items())
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


_LOCKED_REQUIREMENT = re.compile(
    r"^([A-Za-z0-9_.-]+)==([^\s;]+)(?:\s*;\s*(.+))?$"
)
_MARKER_COMPARISON = re.compile(
    r'''^([a-z_]+)\s*(==|!=)\s*['\"]([^'\"]+)['\"]$'''
)


def _marker_applies(marker: str) -> bool:
    """Evaluate the deliberately small marker subset accepted by Core locks."""

    environment = {
        "implementation_name": str(sys.implementation.name),
        "platform_python_implementation": platform.python_implementation(),
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "sys_platform": sys.platform,
        "platform_system": platform.system(),
        "platform_machine": platform.machine(),
    }
    # PEP 508 gives ``and`` higher precedence than ``or``. Parenthesised or
    # ordered/version comparisons are intentionally rejected: the early Core
    # gate must not depend on packaging libraries or guess at lock semantics.
    for disjunction in re.split(r"\s+or\s+", marker.strip()):
        conjunction_matches = True
        for atom in re.split(r"\s+and\s+", disjunction.strip()):
            match = _MARKER_COMPARISON.fullmatch(atom.strip())
            if match is None or match.group(1) not in environment:
                raise RuntimeContractError(
                    f"Unsupported marker in standard dependency lock: {marker!r}"
                )
            actual = environment[match.group(1)]
            expected = match.group(3)
            matched = actual == expected
            if match.group(2) == "!=":
                matched = not matched
            conjunction_matches = conjunction_matches and matched
        if conjunction_matches:
            return True
    return False


def locked_standard_dependencies(code_root: Path, policy: RuntimePolicy) -> dict[str, str]:
    lock_path = Path(code_root).resolve() / policy.standard_lock
    try:
        lines = lock_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeContractError(
            f"HASHI standard dependency lock is unavailable: {lock_path}: {exc}"
        ) from exc
    locked: dict[str, str] = {}
    for line_number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LOCKED_REQUIREMENT.fullmatch(line)
        if match is None:
            raise RuntimeContractError(
                f"Invalid exact requirement in {policy.standard_lock}:{line_number}: {line}"
            )
        if match.group(3) and not _marker_applies(match.group(3)):
            continue
        name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
        locked[name] = match.group(2)
    if not locked:
        raise RuntimeContractError("HASHI standard dependency lock is empty")
    return locked


def validate_standard_dependencies(
    code_root: Path,
    policy: RuntimePolicy,
    *,
    installed: Mapping[str, str] | None = None,
) -> None:
    """Require every standard-profile distribution at its approved version.

    Optional product profiles and development tools may add distributions;
    the complete effective set is still frozen in ``dependency_digest`` for
    every running Core and must match its candidate staging process exactly.
    """

    source_installed = _installed_distributions() if installed is None else installed
    normalized_installed = {
        re.sub(r"[-_.]+", "-", str(name)).lower(): str(version)
        for name, version in source_installed.items()
    }
    failures = []
    for name, expected in locked_standard_dependencies(code_root, policy).items():
        actual = normalized_installed.get(name)
        if actual != expected:
            failures.append(f"{name}: installed={actual or 'missing'}, required={expected}")
    if failures:
        raise RuntimeContractError(
            "HASHI standard dependency generation is not installed exactly: "
            + "; ".join(failures[:20])
        )


def core_source_digest(code_root: Path) -> str:
    root = Path(code_root).resolve()
    digest = hashlib.sha256()
    missing: list[str] = []
    for relative in CORE_SOURCE_PATHS:
        path = root / relative
        if not path.is_file():
            missing.append(relative)
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\n")
    if missing:
        raise RuntimeContractError(
            f"HASHI Core source manifest is incomplete; missing={missing}"
        )
    return "sha256:" + digest.hexdigest()


def current_runtime_fingerprint(
    policy: RuntimePolicy,
    *,
    code_root: Path,
) -> RuntimeFingerprint:
    implementation = str(getattr(sys.implementation, "name", "")).lower()
    cache_tag = str(getattr(sys.implementation, "cache_tag", "") or "")
    executable = str(Path(sys.executable).resolve()) if sys.executable else ""
    # POSIX CPython normally exposes SOABI, while official Windows builds use
    # EXT_SUFFIX as their native-extension ABI identity.  Both are equally
    # valid representations of the platform ABI.
    platform_abi = str(
        sysconfig.get_config_var("SOABI")
        or sysconfig.get_config_var("EXT_SUFFIX")
        or ""
    )
    return RuntimeFingerprint(
        implementation=implementation,
        python=platform.python_version(),
        python_minor=f"{sys.version_info.major}.{sys.version_info.minor}",
        cache_tag=cache_tag,
        platform_abi=platform_abi,
        platform=platform.system().lower(),
        machine=platform.machine().lower(),
        pointer_bits=struct.calcsize("P") * 8,
        executable=executable,
        environment_prefix=str(Path(sys.prefix).resolve()),
        dependency_digest=dependency_digest(),
        core_source_digest=core_source_digest(code_root),
        core_api=policy.core_api,
        function_api=policy.function_api,
    )


def validate_runtime_policy(
    policy: RuntimePolicy,
    fingerprint: RuntimeFingerprint,
) -> None:
    expected_minor = policy.python_minor_text
    failures: list[str] = []
    if fingerprint.implementation != policy.implementation:
        failures.append(
            f"implementation={fingerprint.implementation or 'unknown'} "
            f"(required {policy.implementation})"
        )
    if fingerprint.python_minor != expected_minor:
        failures.append(
            f"python={fingerprint.python} (required {expected_minor}.x)"
        )
    elif fingerprint.python != policy.python_text:
        failures.append(
            f"python={fingerprint.python} (approved production patch {policy.python_text})"
        )
    if not fingerprint.cache_tag:
        failures.append("cache_tag is unavailable")
    if not fingerprint.platform_abi:
        failures.append("platform ABI is unavailable")
    if fingerprint.pointer_bits not in {32, 64}:
        failures.append(f"pointer_bits={fingerprint.pointer_bits}")
    if failures:
        raise RuntimeContractError(
            "HASHI Core runtime contract rejected this process: "
            + "; ".join(failures)
            + ". Rebuild the HASHI environment with the mandated interpreter; "
            "a function /reboot cannot change the Core runtime."
        )


def compare_runtime_fingerprints(
    core: RuntimeFingerprint,
    candidate: RuntimeFingerprint,
) -> None:
    """Require candidate code to use the exact running Core ABI/environment."""

    compared_fields = (
        "implementation",
        "python",
        "python_minor",
        "cache_tag",
        "platform_abi",
        "platform",
        "machine",
        "pointer_bits",
        "executable",
        "environment_prefix",
        "dependency_digest",
        "core_source_digest",
        "core_api",
        "function_api",
    )
    mismatches = [
        f"{field}: core={getattr(core, field)!r}, candidate={getattr(candidate, field)!r}"
        for field in compared_fields
        if getattr(core, field) != getattr(candidate, field)
    ]
    if mismatches:
        raise RuntimeContractError(
            "Function generation does not match the running Core runtime: "
            + "; ".join(mismatches)
        )


def enforce_runtime_contract(
    code_root: Path,
    *,
    require_standard_dependencies: bool = True,
) -> RuntimeFingerprint:
    policy = load_runtime_policy(code_root)
    fingerprint = current_runtime_fingerprint(policy, code_root=code_root)
    validate_runtime_policy(policy, fingerprint)
    if require_standard_dependencies:
        validate_standard_dependencies(code_root, policy)
    return fingerprint


def fingerprint_json(fingerprint: RuntimeFingerprint) -> str:
    return json.dumps(
        fingerprint.to_dict(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
