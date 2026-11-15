"""Authoritative HASHI Core runtime compatibility contract.

This module is deliberately standard-library only.  It is imported before any
function-layer module so an unsupported interpreter cannot partially construct
the HASHI process.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import stat
import platform
import re
import struct
import sys
import sysconfig
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any


CORE_HOST_ROLE = "core-host"
FUNCTIONS_ROLE = "functions"
_RUNTIME_ROLES = frozenset({CORE_HOST_ROLE, FUNCTIONS_ROLE})
_PROCESS_RUNTIME = None
_VERIFICATION_STATS_LOCK = threading.Lock()
_VERIFICATION_STATS = {
    "deep_scan_count": 0,
    "deep_files_observed": 0,
    "deep_files_hashed": 0,
    "deep_bytes_hashed": 0,
    "deep_cache_extras_ignored": 0,
    "deep_duration_ms": 0.0,
    "fast_admission_count": 0,
    "fast_files_hashed": 0,
    "fast_bytes_hashed": 0,
}


class RuntimeContractError(RuntimeError):
    """The process or candidate generation violates the Core runtime contract."""


CORE_SOURCE_PATHS = (
    "__main__.py",
    "main.py",
    "orchestrator/runtime_contract.py",
    "orchestrator/instance_lock.py",
    "orchestrator/kernel_artifact.py",
    "orchestrator/kernel_import_guard.py",
    "orchestrator/kernel_process.py",
    "orchestrator/runtime_process.py",
    "orchestrator/function_worker_bootstrap.py",
    "orchestrator/function_worker_protocol.py",
)


@dataclass(frozen=True)
class RuntimePolicy:
    implementation: str
    python_version: tuple[int, int, int]
    standard_lock: str
    portable_build_date: str
    core_api: int
    function_api: int
    worker_model: str
    worker_protocol: int
    generation_schema: int
    role: str = FUNCTIONS_ROLE
    policy_root: str = ""

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
    runtime_policy_digest: str
    dependency_digest: str
    core_source_digest: str
    core_api: int
    function_api: int
    worker_model: str
    worker_protocol: int
    generation_schema: int
    role: str = FUNCTIONS_ROLE
    installation_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RuntimeFingerprint:
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise RuntimeContractError("Runtime fingerprint schema mismatch")
        integer_fields = {"pointer_bits", "core_api", "function_api", "worker_protocol", "generation_schema"}
        for name, item in value.items():
            if type(item) is not (int if name in integer_fields else str):
                raise RuntimeContractError(f"Runtime fingerprint field type mismatch: {name}")
        role = value.get("role")
        if role not in _RUNTIME_ROLES:
            raise RuntimeContractError("Runtime fingerprint has a missing or invalid role")
        return cls(
            role=str(role),
            installation_digest=str(value.get("installation_digest") or ""),
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
            runtime_policy_digest=str(value["runtime_policy_digest"]),
            dependency_digest=str(value["dependency_digest"]),
            core_source_digest=str(value["core_source_digest"]),
            core_api=int(value["core_api"]),
            function_api=int(value["function_api"]),
            worker_model=str(value["worker_model"]),
            worker_protocol=int(value["worker_protocol"]),
            generation_schema=int(value["generation_schema"]),
        )

    @property
    def runtime_id(self) -> str:
        if self.installation_digest:
            if re.fullmatch(r"sha256:[0-9a-f]{64}", self.installation_digest) is None:
                raise RuntimeContractError("Invalid runtime installation identity")
            prefix = "cr" if self.role == CORE_HOST_ROLE else "fr"
            return prefix + ":" + self.installation_digest
        # A runtime revision is independent of protected code and Function code.
        # Exact code/protocol equality is still checked by admission separately.
        fields = (
            "role", "implementation", "python", "cache_tag", "platform_abi",
            "platform", "machine", "pointer_bits", "executable",
            "environment_prefix", "runtime_policy_digest", "dependency_digest",
        )
        payload = json.dumps(
            {field: getattr(self, field) for field in fields},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")
        prefix = "cr" if self.role == CORE_HOST_ROLE else "fr"
        return prefix + ":sha256:" + hashlib.sha256(payload).hexdigest()


_RUNTIME_SECTION = re.compile(
    r"(?ms)^\[tool\.hashi\.runtime\]\s*$\n(?P<body>.*?)(?=^\[|\Z)"
)
_STRING_SETTING = re.compile(r'(?m)^\s*([a-z][a-z0-9_-]*)\s*=\s*"([^"]+)"\s*$')
_INTEGER_SETTING = re.compile(r"(?m)^\s*([a-z][a-z0-9_-]*)\s*=\s*([0-9]+)\s*$")


def _read_runtime_policy(
    code_root: Path, *, role: str = FUNCTIONS_ROLE
) -> RuntimePolicy:
    """Read the single machine-readable policy from ``pyproject.toml``.

    A tiny parser is used intentionally: an old/unsupported Python must still
    be able to print the required version instead of failing first because
    ``tomllib`` is unavailable.
    """

    if role not in _RUNTIME_ROLES:
        raise RuntimeContractError(f"Unsupported runtime role: {role!r}")
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
        "worker-model",
        "worker-protocol",
        "generation-schema",
    }
    if role == CORE_HOST_ROLE:
        required.add("core-host-lock")
    missing = sorted(required - values.keys())
    if missing:
        raise RuntimeContractError(
            f"HASHI runtime policy is incomplete; missing={missing}"
        )
    approved_python = (
        values.get("functions-python", values["python"])
        if role == FUNCTIONS_ROLE else values["python"]
    )
    version_match = re.fullmatch(
        r"([0-9]+)\.([0-9]+)\.([0-9]+)", approved_python
    )
    if version_match is None:
        raise RuntimeContractError(
            "tool.hashi.runtime.python must be an exact major.minor.patch value"
        )
    lock_setting = "core-host-lock" if role == CORE_HOST_ROLE else "standard-lock"
    standard_lock = values[lock_setting].strip()
    lock_path = Path(standard_lock)
    if not standard_lock or lock_path.is_absolute() or ".." in lock_path.parts:
        raise RuntimeContractError(
            "tool.hashi.runtime.standard-lock must be a repository-relative path"
        )
    portable_build_date = (
        values.get("functions-build-date", values["portable-build-date"])
        if role == FUNCTIONS_ROLE else values["portable-build-date"]
    ).strip()
    if re.fullmatch(r"[0-9]{8}", portable_build_date) is None:
        raise RuntimeContractError(
            "tool.hashi.runtime.portable-build-date must be YYYYMMDD"
        )
    return RuntimePolicy(
        role=role,
        implementation=values["implementation"].strip().lower(),
        python_version=tuple(int(part) for part in version_match.groups()),
        standard_lock=standard_lock,
        portable_build_date=portable_build_date,
        core_api=int(values["core-api"]),
        function_api=int(values["function-api"]),
        worker_model=values["worker-model"].strip().lower(),
        worker_protocol=int(values["worker-protocol"]),
        generation_schema=int(values["generation-schema"]),
    )



def load_runtime_policy(code_root: Path, *, role: str = FUNCTIONS_ROLE,
                        runtime_root: Path | None = None) -> RuntimePolicy:
    """Common code ABI comes from source; role-local policy comes from its seal."""
    common = _read_runtime_policy(code_root, role=role)
    if runtime_root is None and _PROCESS_RUNTIME is not None:
        if _PROCESS_RUNTIME.fingerprint.role == role:
            runtime_root = Path(_PROCESS_RUNTIME.fingerprint.environment_prefix)
    profile = Path(runtime_root) / "runtime-policy" if runtime_root is not None else None
    if profile is None or not profile.is_dir():
        return common
    own = _read_runtime_policy(profile, role=role)
    return replace(own, policy_root=str(profile.resolve()),
                   core_api=common.core_api, function_api=common.function_api,
                   worker_model=common.worker_model, worker_protocol=common.worker_protocol,
                   generation_schema=common.generation_schema)


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


def runtime_policy_digest(code_root: Path, policy: RuntimePolicy) -> str:
    """Hash only this role\'s interpreter policy and exact dependency lock."""

    lock_path = Path(policy.policy_root or code_root).resolve() / policy.standard_lock
    try:
        lock_bytes = lock_path.read_bytes()
    except OSError as exc:
        raise RuntimeContractError(
            f"HASHI standard dependency lock is unavailable: {lock_path}: {exc}"
        ) from exc
    payload = {
        "role": policy.role,
        "implementation": policy.implementation,
        "python": policy.python_text,
        "standard_lock": policy.standard_lock,
        "portable_build_date": policy.portable_build_date,
        "lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


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
    lock_path = Path(policy.policy_root or code_root).resolve() / policy.standard_lock
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
    if policy.role == CORE_HOST_ROLE and locked:
        raise RuntimeContractError("HASHI Core host dependency lock must contain no third-party packages")
    if not locked and policy.role != CORE_HOST_ROLE:
        raise RuntimeContractError("HASHI Functions dependency lock is empty")
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
    locked = locked_standard_dependencies(code_root, policy)
    if policy.role == CORE_HOST_ROLE:
        if normalized_installed:
            raise RuntimeContractError(
                "HASHI Core host contains third-party distributions: "
                + ", ".join(sorted(normalized_installed)[:20])
            )
        return
    failures = []
    for name, expected in locked.items():
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
    dependency_digest_override: str | None = None,
    core_source_digest_override: str | None = None,
) -> RuntimeFingerprint:
    implementation = str(getattr(sys.implementation, "name", "")).lower()
    cache_tag = str(getattr(sys.implementation, "cache_tag", "") or "")
    # Keep the interpreter invocation path: resolving a POSIX venv symlink
    # launches the base interpreter and silently loses the selected prefix.
    # Admission separately verifies the resolved OS process image.
    executable = os.path.abspath(sys.executable) if sys.executable else ""
    # POSIX CPython normally exposes SOABI, while official Windows builds use
    # EXT_SUFFIX as their native-extension ABI identity.  Both are equally
    # valid representations of the platform ABI.
    platform_abi = str(
        sysconfig.get_config_var("SOABI")
        or sysconfig.get_config_var("EXT_SUFFIX")
        or ""
    )
    installation = ""
    if _PROCESS_RUNTIME is not None:
        approved = _PROCESS_RUNTIME.fingerprint
        if approved.role == policy.role:
            if approved.executable != executable or approved.environment_prefix != str(Path(sys.prefix).resolve()):
                raise RuntimeContractError("Process runtime binding does not match the observed interpreter")
            installation = approved.installation_digest
    effective_dependency_digest = (
        dependency_digest_override
        if dependency_digest_override is not None
        else dependency_digest()
    )
    if re.fullmatch(r"sha256:[0-9a-f]{64}", effective_dependency_digest) is None:
        raise RuntimeContractError("Invalid admitted dependency digest")
    effective_core_source_digest = (
        core_source_digest_override
        if core_source_digest_override is not None
        else core_source_digest(code_root)
    )
    if re.fullmatch(r"sha256:[0-9a-f]{64}", effective_core_source_digest) is None:
        raise RuntimeContractError("Invalid admitted Core source digest")
    return RuntimeFingerprint(
        installation_digest=installation,
        role=policy.role,
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
        runtime_policy_digest=runtime_policy_digest(code_root, policy),
        dependency_digest=effective_dependency_digest,
        core_source_digest=effective_core_source_digest,
        core_api=policy.core_api,
        function_api=policy.function_api,
        worker_model=policy.worker_model,
        worker_protocol=policy.worker_protocol,
        generation_schema=policy.generation_schema,
    )


def validate_runtime_policy(
    policy: RuntimePolicy,
    fingerprint: RuntimeFingerprint,
) -> None:
    expected_minor = policy.python_minor_text
    failures: list[str] = []
    if fingerprint.role != policy.role or fingerprint.role not in _RUNTIME_ROLES:
        failures.append(f"role={fingerprint.role!r} (required {policy.role!r})")
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
    if fingerprint.core_api != policy.core_api:
        failures.append(
            f"core_api={fingerprint.core_api} (required {policy.core_api})"
        )
    if fingerprint.function_api != policy.function_api:
        failures.append(
            f"function_api={fingerprint.function_api} "
            f"(required {policy.function_api})"
        )
    if policy.worker_model != "per-agent-process":
        failures.append(
            f"policy worker_model={policy.worker_model!r} "
            "(required 'per-agent-process')"
        )
    if fingerprint.worker_model != policy.worker_model:
        failures.append(
            f"worker_model={fingerprint.worker_model!r} "
            f"(required {policy.worker_model!r})"
        )
    if fingerprint.worker_protocol != policy.worker_protocol:
        failures.append(
            f"worker_protocol={fingerprint.worker_protocol} "
            f"(required {policy.worker_protocol})"
        )
    if fingerprint.generation_schema != policy.generation_schema:
        failures.append(
            f"generation_schema={fingerprint.generation_schema} "
            f"(required {policy.generation_schema})"
        )
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
    """Require exact fingerprint equality within one qualified runtime role."""

    compared_fields = (
        "role",
        "installation_digest",
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
        "runtime_policy_digest",
        "dependency_digest",
        "core_source_digest",
        "core_api",
        "function_api",
        "worker_model",
        "worker_protocol",
        "generation_schema",
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


def compare_runtime_boundary(
    core: RuntimeFingerprint,
    candidate: RuntimeFingerprint,
    *,
    expected_functions: RuntimeFingerprint,
) -> None:
    """Admit an exact approved FR across the distinct Core-host boundary.

    The expected Function fingerprint comes from the trusted release receipt,
    never an Agent-supplied executable/prefix. This primitive does not select
    a runtime or authorize a launch. Different role-local dependency/policy
    digests and interpreter paths are required to remain independent.
    """
    if core.role != CORE_HOST_ROLE:
        raise RuntimeContractError("Core boundary requires the core-host role")
    if candidate.role != FUNCTIONS_ROLE or expected_functions.role != FUNCTIONS_ROLE:
        raise RuntimeContractError("Core boundary requires the functions role")
    compare_runtime_fingerprints(expected_functions, candidate)
    fields = (
        "implementation", "python_minor", "cache_tag", "platform_abi",
        "platform", "machine", "pointer_bits", "core_source_digest",
        "core_api", "function_api", "worker_model", "worker_protocol",
        "generation_schema",
    )
    mismatches = [
        f"{field}: core={getattr(core, field)!r}, functions={getattr(candidate, field)!r}"
        for field in fields if getattr(core, field) != getattr(candidate, field)
    ]
    if mismatches:
        raise RuntimeContractError(
            "Core/Functions boundary is incompatible: " + "; ".join(mismatches)
        )


def enforce_runtime_contract(
    code_root: Path,
    *,
    require_standard_dependencies: bool = True,
    role: str = FUNCTIONS_ROLE,
) -> RuntimeFingerprint:
    policy = load_runtime_policy(code_root, role=role, runtime_root=Path(sys.prefix))
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
# HASHI-Fingerprint: hfp_8d3b6a326d8d4b74b7947ef9db5956e8

@dataclass(frozen=True)
class QualifiedRuntime:
    """A verified installation receipt; never an Agent runtime selector."""

    manifest_path: Path
    manifest_digest: str
    fingerprint: RuntimeFingerprint

    @property
    def executable(self) -> Path:
        return Path(self.fingerprint.executable)

    @property
    def runtime_id(self) -> str:
        return self.fingerprint.runtime_id


def runtime_verification_stats(*, reset: bool = False) -> dict[str, int | float]:
    """Return process-local deep-scan and fast-admission measurements."""
    with _VERIFICATION_STATS_LOCK:
        result = dict(_VERIFICATION_STATS)
        if reset:
            for name in _VERIFICATION_STATS:
                _VERIFICATION_STATS[name] = 0.0 if name.endswith("_ms") else 0
        return result


def _record_verification_stats(**values: int | float) -> None:
    with _VERIFICATION_STATS_LOCK:
        for name, value in values.items():
            _VERIFICATION_STATS[name] += value


def _validate_runtime_file_table(table: Any) -> None:
    if not isinstance(table, dict) or not table:
        raise RuntimeContractError("Runtime installation requires a nonempty file seal")
    for relative, checksum in table.items():
        if (not isinstance(relative, str) or not relative or "\\" in relative
                or ":" in relative or relative.startswith("/")
                or any(part in {"", ".", ".."} for part in relative.split("/"))):
            raise RuntimeContractError("Invalid path in runtime installation manifest")
        if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
            raise RuntimeContractError("Invalid runtime installation file hash")
        if relative.lower().endswith(".pth"):
            raise RuntimeContractError("Runtime installation may not inject site paths with .pth")
        if Path(relative).name.lower() in {"sitecustomize.py", "usercustomize.py",
                                         "sitecustomize.pyc", "usercustomize.pyc"}:
            raise RuntimeContractError("Runtime installation may not inject startup hooks")


_CACHE_FILE = re.compile(r"^(?P<source>.+)\.[^.]+\.pyc$")
_OPTIMIZED_CACHE_FILE = re.compile(
    r"^(?P<source>.+)\.[^.]+\.opt-[0-9]+\.pyc$"
)


def _is_incidental_python_cache(relative: str, table: Mapping[str, str]) -> bool:
    """Allow only PEP 3147 caches backed by an authoritative sealed source."""
    path = Path(relative)
    if path.suffix.lower() != ".pyc" or path.parent.name != "__pycache__":
        return False
    match = _OPTIMIZED_CACHE_FILE.fullmatch(path.name) or _CACHE_FILE.fullmatch(
        path.name
    )
    if match is None:
        return False
    source = (path.parent.parent / (match.group("source") + ".py")).as_posix()
    return source in table and Path(source).name.lower() not in {
        "sitecustomize.py", "usercustomize.py"
    }


def verify_runtime_files(root: Path, table: Any, *, manifest_path: Path) -> None:
    """Deeply verify authoritative bytes and the installation closure.

    Additional PEP 3147 caches are incidental runtime products and are not
    authoritative installation files. Every other additional file remains a
    closure failure. The -B flag prevents HASHI writes but does not make
    bytecode harmless; explicit deep audits report the cache exception.
    """
    started = time.perf_counter()
    root = Path(root)
    _validate_runtime_file_table(table)
    observed = {}
    pending = [(root, "")]
    excluded = manifest_path.relative_to(root).as_posix() if manifest_path.is_relative_to(root) else None
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                metadata = entry.stat(follow_symlinks=False)
                if os.name == "nt" and metadata.st_ino == 0:
                    metadata = os.stat(entry.path, follow_symlinks=False)
                if (stat.S_ISLNK(metadata.st_mode)
                        or getattr(metadata, "st_file_attributes", 0) & 0x400):
                    raise RuntimeContractError("Runtime installation contains an unsealed redirected path")
                relative = prefix + entry.name
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append((Path(entry.path), relative + "/"))
                elif stat.S_ISREG(metadata.st_mode):
                    if relative != excluded:
                        observed[relative] = (Path(entry.path), metadata)
                else:
                    raise RuntimeContractError("Runtime installation contains a non-regular entry")
    extra = set(observed) - set(table)
    incidental = {
        relative for relative in extra
        if _is_incidental_python_cache(relative, table)
    }
    unexpected = extra - incidental
    missing = set(table) - set(observed)
    if unexpected or missing:
        raise RuntimeContractError(
            "Runtime installation file closure changed: "
            f"unlisted={sorted(unexpected)[:10]}, missing={sorted(missing)[:10]}")

    def identity(value):
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    bytes_hashed = 0
    for relative in table:
        file, expected_stat = observed[relative]
        digest = hashlib.sha256()
        with file.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if identity(before) != identity(expected_stat):
                raise RuntimeContractError("Runtime installation file changed during verification")
            while block := stream.read(1024 * 1024):
                digest.update(block)
                bytes_hashed += len(block)
            if identity(os.fstat(stream.fileno())) != identity(before):
                raise RuntimeContractError("Runtime installation file changed during verification")
        if digest.hexdigest() != table[relative]:
            raise RuntimeContractError(f"Runtime installation file hash mismatch: {relative}")
    _record_verification_stats(
        deep_scan_count=1,
        deep_files_observed=len(observed),
        deep_files_hashed=len(table),
        deep_bytes_hashed=bytes_hashed,
        deep_cache_extras_ignored=len(incidental),
        deep_duration_ms=(time.perf_counter() - started) * 1000,
    )


def _hash_sealed_file(root: Path, table: Mapping[str, str], relative: str) -> int:
    path = root / relative
    if (relative not in table or path.is_symlink() or not path.is_file()
            or not path.resolve().is_relative_to(root)):
        raise RuntimeContractError(f"Required runtime file is unavailable: {relative}")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != table[relative]:
        raise RuntimeContractError(f"Runtime installation file hash mismatch: {relative}")
    return len(payload)


def _verify_shallow_import_root(
    root: Path,
    table: Mapping[str, str],
    relative_root: str,
    *,
    manifest_path: Path,
    declared_children: frozenset[str] = frozenset(),
) -> None:
    """Reject new top-level import/startup entries without a recursive scan."""
    directory = root / relative_root if relative_root else root
    if not directory.is_dir():
        return
    prefix = relative_root.rstrip("/")
    prefix = prefix + "/" if prefix else ""
    allowed = {
        relative[len(prefix):].split("/", 1)[0]
        for relative in table
        if relative.startswith(prefix) and relative != prefix.rstrip("/")
    }
    if manifest_path.is_relative_to(directory):
        allowed.add(manifest_path.relative_to(directory).parts[0])
    allowed.update(declared_children)
    for entry in os.scandir(directory):
        if entry.name in allowed or entry.name == "__pycache__":
            continue
        metadata = entry.stat(follow_symlinks=False)
        if (
            stat.S_ISDIR(metadata.st_mode)
            and not getattr(metadata, "st_file_attributes", 0) & 0x400
        ):
            # venv creates empty scaffold directories such as Include and Lib
            # that contain no authoritative files and therefore do not appear
            # in the file seal.  Empty directories cannot supply a module or
            # native loader.  A later populated import root is still rejected
            # by its own shallow scan or by this nonempty-directory check.
            with os.scandir(entry.path) as children:
                if next(children, None) is None:
                    continue
        raise RuntimeContractError(
            f"Runtime import root contains an unsealed top-level entry: "
            f"{prefix}{entry.name}"
        )


def _fast_runtime_file_admission(
    root: Path,
    table: Mapping[str, str],
    *,
    manifest_path: Path,
    required: tuple[str, ...],
    import_roots: tuple[str, ...],
) -> tuple[int, int]:
    _validate_runtime_file_table(table)
    hashed = 0
    bytes_hashed = 0
    for relative in dict.fromkeys(required):
        bytes_hashed += _hash_sealed_file(root, table, relative)
        hashed += 1
    import_root_parts = tuple(
        (relative_root, PurePosixPath(relative_root).parts)
        for relative_root in dict.fromkeys(import_roots)
    )
    for relative_root, current_parts in import_root_parts:
        declared_children = frozenset(
            candidate_parts[len(current_parts)]
            for _, candidate_parts in import_root_parts
            if (
                len(candidate_parts) > len(current_parts)
                and candidate_parts[:len(current_parts)] == current_parts
            )
        )
        _verify_shallow_import_root(
            root,
            table,
            relative_root,
            manifest_path=manifest_path,
            declared_children=declared_children,
        )
    return hashed, bytes_hashed


def _load_runtime_receipt(
    manifest_path: Path,
    *,
    expected_digest: str,
    code_root: Path,
    role: str = FUNCTIONS_ROLE,
    deep_verification: bool,
) -> QualifiedRuntime:
    """Load a receipt selected by the trusted installation/release authority.

    This primitive never selects or authorizes a runtime. The caller supplies
    a path and digest from the installation authority, not Agent configuration.
    Deep verification hashes the complete authoritative installation. Fast
    admission hashes only execution-critical files and checks shallow import
    roots; it relies on the previously deep-qualified manifest identity.
    """
    if role not in _RUNTIME_ROLES:
        raise RuntimeContractError("Invalid installation runtime role")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", str(expected_digest)) is None:
        raise RuntimeContractError("Runtime installation requires a trusted manifest digest")
    original = Path(manifest_path)
    if (not original.is_absolute() or original.is_symlink()
            or original.resolve() != original.absolute()):
        raise RuntimeContractError("Runtime manifest path must be absolute and unredirected")
    path = original.resolve()
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            raise RuntimeContractError("Runtime installation manifest is oversized")
        payload = path.read_bytes()
    except OSError as exc:
        raise RuntimeContractError("Runtime installation manifest is unavailable") from exc
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    if digest != expected_digest:
        raise RuntimeContractError("Runtime installation manifest hash mismatch")
    def unique_members(pairs):
        members = {}
        for name, item in pairs:
            if name in members:
                raise RuntimeContractError(f"Runtime installation has a duplicate JSON member: {name}")
            members[name] = item
        return members

    try:
        value = json.loads(payload, object_pairs_hook=unique_members)
    except (ValueError, UnicodeError) as exc:
        raise RuntimeContractError("Runtime installation manifest is invalid JSON") from exc
    required = {"schema_version", "role", "runtime", "environment_files",
                "base_root", "base_files"}
    if not isinstance(value, dict) or set(value) != required:
        raise RuntimeContractError("Runtime installation manifest schema mismatch")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise RuntimeContractError("Runtime installation manifest version mismatch")
    if value["role"] != role:
        raise RuntimeContractError("Runtime installation manifest role mismatch")
    raw = value["runtime"]
    if not isinstance(raw, dict) or set(raw) != set(RuntimeFingerprint.__dataclass_fields__):
        raise RuntimeContractError("Runtime installation fingerprint schema mismatch")
    integers = {"pointer_bits", "core_api", "function_api", "worker_protocol", "generation_schema"}
    for name, item in raw.items():
        if type(item) is not (int if name in integers else str):
            raise RuntimeContractError(f"Invalid runtime fingerprint field type: {name}")
    if raw["installation_digest"]:
        raise RuntimeContractError("Runtime installation cannot self-assert its derived identity")
    for name in ("runtime_policy_digest", "dependency_digest", "core_source_digest"):
        if re.fullmatch(r"sha256:[0-9a-f]{64}", raw[name]) is None:
            raise RuntimeContractError(f"Invalid runtime fingerprint digest: {name}")
    runtime = RuntimeFingerprint.from_mapping(raw)
    if runtime.role != role:
        raise RuntimeContractError("Runtime installation fingerprint role mismatch")
    environment = Path(runtime.environment_prefix)
    if (not environment.is_absolute() or not environment.is_dir()
            or environment.is_symlink() or environment.resolve() != environment.absolute()):
        raise RuntimeContractError("Runtime installation environment prefix mismatch")
    root = environment.resolve()
    executable = Path(runtime.executable)
    if not executable.is_absolute() or not executable.is_relative_to(root):
        raise RuntimeContractError("Runtime executable is outside its approved environment")
    if not isinstance(value["environment_files"], dict) or not isinstance(value["base_files"], dict):
        raise RuntimeContractError("Runtime installation file tables must be objects")
    environment_files = value["environment_files"]
    base_files = value["base_files"]
    executable_relative = executable.relative_to(root).as_posix()
    if executable_relative not in environment_files:
        raise RuntimeContractError("Runtime executable is not sealed by its installation")
    fast_files = 0
    fast_bytes = 0
    if deep_verification:
        verify_runtime_files(root, environment_files, manifest_path=path)
    else:
        site_relative = (
            "Lib/site-packages"
            if runtime.platform == "windows"
            else f"lib/python{runtime.python_minor}/site-packages"
        )
        standard_relative = (
            "Lib"
            if runtime.platform == "windows"
            else f"lib/python{runtime.python_minor}"
        )
        required = (executable_relative,) + (
            ("pyvenv.cfg",) if "pyvenv.cfg" in environment_files else ()
        )
        count, size = _fast_runtime_file_admission(
            root,
            environment_files,
            manifest_path=path,
            required=required,
            import_roots=("", standard_relative, site_relative),
        )
        fast_files += count
        fast_bytes += size
    policy = load_runtime_policy(code_root, role=role, runtime_root=root)
    validate_runtime_policy(policy, runtime)
    observed = current_runtime_fingerprint(
        policy,
        code_root=code_root,
        dependency_digest_override=(
            None if deep_verification else runtime.dependency_digest
        ),
        core_source_digest_override=(
            None if deep_verification else runtime.core_source_digest
        ),
    )
    fields = ("implementation", "python_minor", "cache_tag", "platform_abi",
              "platform", "machine", "pointer_bits", "runtime_policy_digest", "core_source_digest")
    for field in fields:
        if getattr(runtime, field) != getattr(observed, field):
            raise RuntimeContractError(f"Runtime installation host/policy mismatch: {field}")
    if not isinstance(value["base_root"], str):
        raise RuntimeContractError("Runtime CPython base path is invalid")
    base_root = Path(value["base_root"])
    if not base_root.is_absolute() or base_root.is_symlink():
        raise RuntimeContractError("Runtime CPython base path is invalid")
    base_root = base_root.resolve()
    if deep_verification:
        verify_runtime_files(base_root, base_files, manifest_path=path)
    else:
        base_candidates = (
            ("python.exe",)
            if runtime.platform == "windows"
            else (
                f"bin/python{runtime.python_minor}",
                "bin/python3",
                "bin/python",
            )
        )
        base_executable = next(
            (relative for relative in base_candidates if relative in base_files),
            None,
        )
        if base_executable is None:
            raise RuntimeContractError("Runtime CPython base interpreter is not sealed")
        standard_relative = (
            "Lib"
            if runtime.platform == "windows"
            else f"lib/python{runtime.python_minor}"
        )
        count, size = _fast_runtime_file_admission(
            base_root,
            base_files,
            manifest_path=path,
            required=(base_executable,),
            import_roots=("", standard_relative),
        )
        fast_files += count
        fast_bytes += size
    config = root / "pyvenv.cfg"
    if config.is_file():
        settings = {}
        for line in config.read_text(encoding="utf-8").splitlines():
            name, separator, text = line.partition("=")
            if separator:
                settings[name.strip().lower()] = text.strip()
        if settings.get("include-system-site-packages", "").lower() != "false":
            raise RuntimeContractError("Runtime installation enables system site packages")
        approved_homes = {base_root}
        if runtime.platform != "windows":
            approved_homes.add(base_root / "bin")
        if Path(settings.get("home", "")).resolve() not in approved_homes:
            raise RuntimeContractError("Runtime installation CPython base does not match its seal")
    elif base_root != root:
        raise RuntimeContractError("Runtime installation is missing its venv base binding")
    if deep_verification:
        site_path = root / ("Lib/site-packages" if runtime.platform == "windows"
                            else f"lib/python{runtime.python_minor}/site-packages")
        installed = {}
        for distribution in importlib.metadata.distributions(path=[str(site_path)]):
            name = re.sub(r"[-_.]+", "-", str(distribution.metadata.get("Name") or "")).lower()
            if not name or name in installed:
                raise RuntimeContractError("Runtime installation distribution metadata is ambiguous")
            installed[name] = str(distribution.version)
        validate_standard_dependencies(code_root, policy, installed=installed)
        packages = "\n".join(
            f"{name}=={version}" for name, version in sorted(installed.items())
        )
        if "sha256:" + hashlib.sha256(packages.encode()).hexdigest() != runtime.dependency_digest:
            raise RuntimeContractError("Runtime installation dependency digest mismatch")
    # Code/protocol qualification is separate from the immutable CR/FR identity.
    identity_fields = ("role", "implementation", "python", "cache_tag", "platform_abi",
                       "platform", "machine", "pointer_bits", "runtime_policy_digest", "dependency_digest")
    identity = {"profile": {name: raw[name] for name in identity_fields},
                "environment_files": value["environment_files"], "base_files": value["base_files"]}
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    runtime = replace(runtime, installation_digest="sha256:" + hashlib.sha256(encoded).hexdigest())
    if not deep_verification:
        _record_verification_stats(
            fast_admission_count=1,
            fast_files_hashed=fast_files,
            fast_bytes_hashed=fast_bytes,
        )
    return QualifiedRuntime(path, digest, runtime)


def load_qualified_runtime(
    manifest_path: Path,
    *,
    expected_digest: str,
    code_root: Path,
    role: str = FUNCTIONS_ROLE,
) -> QualifiedRuntime:
    """Deep installation verification for install, adoption, and audit flows."""
    return _load_runtime_receipt(
        manifest_path,
        expected_digest=expected_digest,
        code_root=code_root,
        role=role,
        deep_verification=True,
    )


def admit_qualified_runtime(
    manifest_path: Path,
    *,
    expected_digest: str,
    code_root: Path,
    role: str = FUNCTIONS_ROLE,
) -> QualifiedRuntime:
    """Fast admission of a runtime already selected and deeply qualified."""
    return _load_runtime_receipt(
        manifest_path,
        expected_digest=expected_digest,
        code_root=code_root,
        role=role,
        deep_verification=False,
    )


def current_process_runtime() -> QualifiedRuntime | None:
    """Projection of the installation admitted over the private parent channel."""
    return _PROCESS_RUNTIME


def admitted_process_runtime_fingerprint(
    qualified: QualifiedRuntime,
    *,
    code_root: Path,
) -> RuntimeFingerprint:
    """Observe process/ABI/code facts while reusing qualified dependency identity."""
    policy = load_runtime_policy(
        code_root,
        role=qualified.fingerprint.role,
        runtime_root=Path(qualified.fingerprint.environment_prefix),
    )
    validate_runtime_policy(policy, qualified.fingerprint)
    observed = current_runtime_fingerprint(
        policy,
        code_root=code_root,
        dependency_digest_override=qualified.fingerprint.dependency_digest,
        core_source_digest_override=qualified.fingerprint.core_source_digest,
    )
    return replace(
        observed,
        installation_digest=qualified.fingerprint.installation_digest,
    )


def bind_process_runtime(
    qualified: QualifiedRuntime,
    *,
    code_root: Path,
) -> RuntimeFingerprint:
    """Bind once after observing this interpreter; never select from Agent/env input."""
    global _PROCESS_RUNTIME
    observed = admitted_process_runtime_fingerprint(qualified, code_root=code_root)
    compare_runtime_fingerprints(qualified.fingerprint, observed)
    if _PROCESS_RUNTIME is not None:
        compare_runtime_fingerprints(_PROCESS_RUNTIME.fingerprint, qualified.fingerprint)
    _PROCESS_RUNTIME = qualified
    return observed


@dataclass(frozen=True)
class RuntimeSelection:
    core: QualifiedRuntime
    functions: QualifiedRuntime


def runtime_installation_reference(runtime: QualifiedRuntime) -> dict[str, str]:
    return {"path": str(runtime.manifest_path), "digest": runtime.manifest_digest}


def qualified_runtime_from_reference(reference, *, code_root: Path, role=FUNCTIONS_ROLE):
    if not isinstance(reference, dict) or set(reference) != {"path", "digest"}:
        raise RuntimeContractError("Invalid trusted installation reference")
    if not all(isinstance(value, str) and value for value in reference.values()):
        raise RuntimeContractError("Invalid trusted installation reference fields")
    return load_qualified_runtime(Path(reference["path"]), expected_digest=reference["digest"],
                                  code_root=code_root, role=role)


def admitted_runtime_from_reference(reference, *, code_root: Path, role=FUNCTIONS_ROLE):
    if not isinstance(reference, dict) or set(reference) != {"path", "digest"}:
        raise RuntimeContractError("Invalid trusted installation reference")
    if not all(isinstance(value, str) and value for value in reference.values()):
        raise RuntimeContractError("Invalid trusted installation reference fields")
    return admit_qualified_runtime(
        Path(reference["path"]),
        expected_digest=reference["digest"],
        code_root=code_root,
        role=role,
    )


def load_core_runtime_selection(code_root: Path) -> RuntimeSelection:
    """Read only the installation-authority location anchored to this Core prefix.

    There is deliberately no environment, Agent config, Workzone or command-line
    executable override. Updating this operator-owned record does not itself
    adopt a release; the existing explicitly scoped replacement must still run.
    """
    path = Path(sys.prefix).resolve().parent / "hashi-runtime-release.json"
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise RuntimeContractError("Runtime release selector is redirected")
    try:
        if path.stat().st_size > 65536:
            raise RuntimeContractError("Runtime release selector is oversized")
        payload = path.read_bytes()
    except OSError as exc:
        raise RuntimeContractError("Missing trusted Core/Functions runtime release selector") from exc
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeContractError("Duplicate runtime release selector member")
            result[key] = value
        return result
    try:
        record = json.loads(payload, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as exc:
        raise RuntimeContractError("Invalid runtime release selector JSON") from exc
    if (not isinstance(record, dict) or set(record) != {"schema_version", "core", "functions"}
            or type(record["schema_version"]) is not int or record["schema_version"] != 1):
        raise RuntimeContractError("Invalid runtime release selector schema")
    core = admitted_runtime_from_reference(
        record["core"], code_root=code_root, role=CORE_HOST_ROLE
    )
    functions = admitted_runtime_from_reference(
        record["functions"], code_root=code_root
    )
    compare_runtime_boundary(core.fingerprint, functions.fingerprint,
                             expected_functions=functions.fingerprint)
    return RuntimeSelection(core, functions)
