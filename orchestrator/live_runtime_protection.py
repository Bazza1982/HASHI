"""Precise tool admission for a running HASHI Core environment.

The operating-system deployment boundary is authoritative.  This module adds
an early, actionable denial for Agent tools without widening protection to the
Agent's workzone or to independent development environments.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from orchestrator.runtime_contract import CORE_SOURCE_PATHS


POLICY_SCHEMA = 1
POLICY_RELATIVE_PATH = Path("state") / "platform" / "live-runtime-protection.json"

_PATH_WRITE_TOOLS = frozenset({"file_write", "apply_patch"})
_PATH_READ_TOOLS = frozenset({"file_read", "log_query"})
_COMMAND_TOOLS = frozenset(
    {
        "bash",
        "shell",
        "background_job_start",
        "managed_process_start",
        "verification_run",
    }
)
_PACKAGE_MUTATIONS = frozenset(
    {"add", "install", "remove", "sync", "uninstall", "upgrade"}
)
_MUTATION_MARKERS = frozenset(
    {
        "add-content",
        "attrib",
        "chattr",
        "chmod",
        "chown",
        "copy",
        "copy-item",
        "cp",
        "del",
        "erase",
        "git-apply",
        "git-checkout",
        "git-clean",
        "git-mv",
        "git-reset",
        "git-restore",
        "git-rm",
        "icacls",
        "install",
        "move",
        "move-item",
        "mv",
        "new-item",
        "out-file",
        "patch",
        "remove-item",
        "rename-item",
        "rm",
        "sed",
        "set-acl",
        "set-content",
        "takeown",
        "tee",
        "touch",
        "truncate",
        "write_text",
        "write_bytes",
    }
)
_SAFE_INSPECTION_PREFIXES = (
    "cat ",
    "dir ",
    "get-acl ",
    "get-content ",
    "get-filehash ",
    "get-item ",
    "git diff ",
    "git show ",
    "git status ",
    "ls ",
    "readlink ",
    "rg ",
    "sha256sum ",
    "stat ",
    "test-path ",
    "type ",
)
_SERVICE_MUTATION_RE = re.compile(
    r"(?:\b(?:restart|start|stop|set)-service\b|"
    r"\bsc(?:\.exe)?\s+(?:config|continue|delete|failure|pause|start|stop)\b|"
    r"\bsystemctl\b[^\r\n;&|]*\b(?:disable|edit|enable|mask|reload|restart|start|stop|unmask)\b|"
    r"\bservice\b[^\r\n;&|]*\b(?:reload|restart|start|stop)\b|"
    r"\bnet\s+(?:start|stop)\b)",
    re.IGNORECASE,
)
_PROCESS_MUTATION_RE = re.compile(
    r"(?:\bkill\b|\bpkill\b|\btaskkill(?:\.exe)?\b|\bstop-process\b)",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r'''"[^"]*"|'[^']*'|[^\s;&|]+''')


@dataclass(frozen=True)
class LiveRuntimePolicy:
    code_root: Path
    bridge_home: Path
    runtime_roots: tuple[Path, ...]
    protected_write_paths: tuple[Path, ...]
    protected_read_paths: tuple[Path, ...]
    service_targets: tuple[str, ...]
    core_pid: int | None
    policy_path: Path
    configuration_error: str | None = None


@dataclass(frozen=True)
class ProtectionDecision:
    operation: str
    target: str
    explanation: str

    def details(self) -> dict[str, Any]:
        return {
            "control_disposition": "denied",
            "reason": "live_runtime_protection",
            "operation": self.operation,
            "target": self.target,
            "retryable": False,
        }


def policy_path_for(global_config: Any) -> Path | None:
    bridge_home = _config_path(global_config, "bridge_home")
    if bridge_home is None:
        return None
    return bridge_home / POLICY_RELATIVE_PATH


def policy_cache_key(global_config: Any, runtime_prefix: str | Path | None) -> tuple:
    """Return a cheap cache key that notices an atomically replaced policy."""

    policy_path = policy_path_for(global_config)
    policy_stat: tuple[int, int] | None = None
    if policy_path is not None:
        try:
            stat = policy_path.stat()
            policy_stat = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            policy_stat = None
    return (
        str(_config_path(global_config, "project_root") or ""),
        str(_config_path(global_config, "bridge_home") or ""),
        str(_config_path(global_config, "secrets_path") or ""),
        str(runtime_prefix or ""),
        str(getattr(global_config, "instance_id", "") or ""),
        policy_stat,
    )


def load_live_runtime_policy(
    global_config: Any,
    *,
    runtime_prefix: str | Path | None,
) -> LiveRuntimePolicy | None:
    """Build the narrow live policy from authoritative runtime facts.

    The optional instance-local JSON can only extend the derived baseline.  A
    malformed extension is ignored as a unit while Core, runtime, secret and
    current-instance process protection remain active.
    """

    code_root = _config_path(global_config, "project_root")
    bridge_home = _config_path(global_config, "bridge_home")
    if code_root is None or bridge_home is None:
        return None
    policy_path = bridge_home / POLICY_RELATIVE_PATH
    runtime_roots: list[Path] = []
    if runtime_prefix:
        runtime_roots.append(_absolute_path(runtime_prefix))
    protected_write = [code_root / relative for relative in CORE_SOURCE_PATHS]
    protected_write.append(policy_path)
    protected_read: list[Path] = []
    secrets_path = _config_path(global_config, "secrets_path")
    if secrets_path is not None:
        protected_write.append(secrets_path)
        protected_read.append(secrets_path)
    instance_id = str(getattr(global_config, "instance_id", "") or "").strip()
    service_targets = [instance_id] if instance_id else []
    configuration_error: str | None = None

    if policy_path.is_file():
        try:
            raw = json.loads(policy_path.read_text(encoding="utf-8"))
            extension = _validated_extension(raw)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            configuration_error = f"{type(exc).__name__}: {exc}"
        else:
            runtime_roots.extend(extension["runtime_roots"])
            protected_write.extend(extension["protected_write_paths"])
            protected_read.extend(extension["protected_read_paths"])
            service_targets.extend(extension["service_targets"])

    core_pid = _read_core_pid(bridge_home / "state" / "instance" / "process.pid")
    return LiveRuntimePolicy(
        code_root=code_root,
        bridge_home=bridge_home,
        runtime_roots=_unique_paths(runtime_roots),
        protected_write_paths=_unique_paths(protected_write),
        protected_read_paths=_unique_paths(protected_read),
        service_targets=tuple(
            dict.fromkeys(
                target.casefold()
                for target in service_targets
                if str(target or "").strip()
            )
        ),
        core_pid=core_pid,
        policy_path=policy_path,
        configuration_error=configuration_error,
    )


def evaluate_live_runtime_request(
    policy: LiveRuntimePolicy | None,
    *,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    workspace_dir: Path,
) -> ProtectionDecision | None:
    if policy is None:
        return None
    args = dict(arguments or {})
    if tool_name in _PATH_WRITE_TOOLS:
        candidate = _argument_path(args.get("path"), workspace_dir)
        if candidate is None:
            return None
        target = _write_target(policy, candidate)
        if target is not None:
            return _decision("write", target)
        return None
    if tool_name in _PATH_READ_TOOLS:
        candidate = _argument_path(args.get("path"), workspace_dir)
        if candidate is None:
            return None
        target = _matching_path(candidate, policy.protected_read_paths)
        if target is not None:
            return _decision("read_secret", target)
        return None
    if tool_name == "process_kill":
        try:
            requested_pid = int(args.get("pid"))
        except (TypeError, ValueError):
            return None
        if policy.core_pid is not None and requested_pid == policy.core_pid:
            return _decision("raw_core_process_control", f"pid:{requested_pid}")
        return None
    if tool_name not in _COMMAND_TOOLS:
        return None

    if tool_name == "managed_process_start":
        cwd = _argument_path(args.get("cwd"), workspace_dir)
        if cwd is not None:
            target = _matching_path(
                cwd,
                (*policy.protected_write_paths, *policy.runtime_roots),
            )
            if target is not None:
                return _decision("managed_process_start", target)

    command, tokens = _command_and_tokens(tool_name, args)
    if not command:
        return None
    for target in policy.protected_read_paths:
        if _command_mentions_path(command, target, workspace_dir):
            return _decision("read_secret", target)

    package_target = _live_package_mutation_target(
        command,
        tokens,
        runtime_roots=policy.runtime_roots,
        workspace_dir=workspace_dir,
    )
    if package_target is not None:
        return _decision("live_dependency_mutation", package_target)

    for target in (*policy.protected_write_paths, *policy.runtime_roots):
        if not _command_mentions_path(command, target, workspace_dir):
            continue
        if _safe_inspection(command):
            continue
        return _decision("write", target)

    lowered = command.casefold()
    if _SERVICE_MUTATION_RE.search(command) and any(
        _contains_term(lowered, target) for target in policy.service_targets
    ):
        target = next(
            target
            for target in policy.service_targets
            if _contains_term(lowered, target)
        )
        return _decision("raw_service_control", target)
    if (
        policy.core_pid is not None
        and _PROCESS_MUTATION_RE.search(command)
        and _contains_term(lowered, str(policy.core_pid))
    ):
        return _decision("raw_core_process_control", f"pid:{policy.core_pid}")
    return None


def _validated_extension(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("policy must be a JSON object")
    if raw.get("schema") != POLICY_SCHEMA:
        raise ValueError(f"policy schema must be {POLICY_SCHEMA}")
    allowed = {
        "schema",
        "runtime_roots",
        "protected_write_paths",
        "protected_read_paths",
        "service_targets",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"unknown policy fields: {', '.join(unknown)}")
    result: dict[str, Any] = {}
    for key in (
        "runtime_roots",
        "protected_write_paths",
        "protected_read_paths",
    ):
        values = raw.get(key, [])
        if not isinstance(values, list) or not all(
            isinstance(value, str) and value.strip() for value in values
        ):
            raise ValueError(f"{key} must be a list of non-empty paths")
        paths = tuple(_absolute_path(value, require_absolute=True) for value in values)
        result[key] = paths
    targets = raw.get("service_targets", [])
    if not isinstance(targets, list) or not all(
        isinstance(value, str) and value.strip() for value in targets
    ):
        raise ValueError("service_targets must be a list of non-empty names")
    result["service_targets"] = tuple(value.strip() for value in targets)
    return result


def _config_path(config: Any, name: str) -> Path | None:
    value = getattr(config, name, None)
    if value is None or not str(value).strip():
        return None
    return _absolute_path(value)


def _absolute_path(value: str | Path, *, require_absolute: bool = False) -> Path:
    expanded = Path(os.path.expandvars(str(value))).expanduser()
    if require_absolute and not expanded.is_absolute():
        raise ValueError(f"live protection paths must be absolute: {value!r}")
    return expanded.resolve(strict=False)


def _unique_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    values: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        resolved = path.resolve(strict=False)
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        values.append(resolved)
    return tuple(values)


def _argument_path(raw: Any, workspace_dir: Path) -> Path | None:
    if raw is None or not str(raw).strip():
        return None
    path = Path(os.path.expandvars(str(raw))).expanduser()
    if not path.is_absolute():
        path = Path(workspace_dir) / path
    return path.resolve(strict=False)


def _matching_path(candidate: Path, targets: Sequence[Path]) -> Path | None:
    candidate_key = os.path.normcase(str(candidate.resolve(strict=False)))
    for target in targets:
        target_key = os.path.normcase(str(target.resolve(strict=False)))
        if candidate_key == target_key or candidate_key.startswith(
            target_key.rstrip("\\/") + os.sep
        ):
            return target
    return None


def _write_target(policy: LiveRuntimePolicy, candidate: Path) -> Path | None:
    target = _matching_path(candidate, policy.protected_write_paths)
    if target is not None:
        return target
    return _matching_path(candidate, policy.runtime_roots)


def _read_core_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8")[:128]
    except (OSError, UnicodeError):
        return None
    match = re.search(r"(?<!\d)(\d{1,12})(?!\d)", text)
    if match is None:
        return None
    value = int(match.group(1))
    return value if value > 0 else None


def _command_and_tokens(tool_name: str, arguments: Mapping[str, Any]) -> tuple[str, list[str]]:
    if tool_name == "verification_run" or (
        tool_name == "managed_process_start" and arguments.get("argv") is not None
    ):
        argv = arguments.get("argv")
        if not isinstance(argv, list):
            return "", []
        tokens = [str(value) for value in argv]
        return " ".join(tokens), tokens
    command = str(arguments.get("command") or "").strip()
    tokens = [token.strip("\"'") for token in _TOKEN_RE.findall(command)]
    return command, tokens


def _normalized_command(value: str) -> str:
    return value.casefold().replace("\\", "/")


def _command_mentions_path(command: str, target: Path, workspace_dir: Path) -> bool:
    normalized = _normalized_command(command)
    absolute = _normalized_command(str(target.resolve(strict=False)))
    variants = {absolute, absolute.replace("/", "//")}
    try:
        relative = os.path.relpath(target, Path(workspace_dir).resolve(strict=False))
    except (OSError, ValueError):
        relative = ""
    if relative:
        relative_norm = _normalized_command(relative)
        variants.update({relative_norm, relative_norm.replace("/", "//")})
    return any(variant and variant in normalized for variant in variants)


def _safe_inspection(command: str) -> bool:
    normalized = " ".join(command.casefold().strip().split())
    if not normalized.startswith(_SAFE_INSPECTION_PREFIXES):
        return False
    if re.search(r"(?<![<])>(?![>])|>>", command):
        return False
    tokens = {
        token.strip("\"'(),;").casefold()
        for token in _TOKEN_RE.findall(command)
    }
    if tokens & _MUTATION_MARKERS:
        return False
    git_mutation = any(
        f"git {verb} " in f" {normalized} "
        for verb in ("apply", "checkout", "clean", "mv", "reset", "restore", "rm")
    )
    return not git_mutation


def _live_package_mutation_target(
    command: str,
    tokens: Sequence[str],
    *,
    runtime_roots: Sequence[Path],
    workspace_dir: Path,
) -> Path | str | None:
    lowered = [Path(token).name.casefold() for token in tokens]
    mutation = False
    for index, token in enumerate(lowered):
        if token in {"pip", "pip.exe", "pip3", "pip3.exe"}:
            if any(value in _PACKAGE_MUTATIONS for value in lowered[index + 1 :]):
                mutation = True
                break
        if token in {"uv", "uv.exe"}:
            tail = lowered[index + 1 :]
            if tail and (
                tail[0] in {"add", "remove", "sync"}
                or (
                    tail[0] == "pip"
                    and any(value in _PACKAGE_MUTATIONS for value in tail[1:])
                )
            ):
                mutation = True
                break
    if not mutation:
        return None

    explicit_paths: list[Path] = []
    for index, token in enumerate(tokens):
        clean = token.strip("\"'")
        name = Path(clean).name.casefold()
        if name in {"python", "python.exe", "python3", "python3.exe", "pip", "pip.exe", "pip3", "pip3.exe"}:
            if Path(clean).is_absolute() or "/" in clean or "\\" in clean:
                candidate = _argument_path(clean, workspace_dir)
                if candidate is not None:
                    explicit_paths.append(candidate)
        if clean.casefold() in {"--python", "--project"} and index + 1 < len(tokens):
            candidate = _argument_path(tokens[index + 1].strip("\"'"), workspace_dir)
            if candidate is not None:
                explicit_paths.append(candidate)
    for candidate in explicit_paths:
        target = _matching_path(candidate, runtime_roots)
        if target is not None:
            return target
    if explicit_paths:
        return None
    return runtime_roots[0] if runtime_roots else "active Core interpreter"


def _contains_term(text: str, term: str) -> bool:
    value = str(term or "").strip().casefold()
    if not value:
        return False
    return re.search(
        rf"(?<![a-z0-9_]){re.escape(value)}(?![a-z0-9_])", text
    ) is not None


def _decision(operation: str, target: str | Path) -> ProtectionDecision:
    rendered = str(target)
    return ProtectionDecision(
        operation=operation,
        target=rendered,
        explanation=(
            f"Agent tool access to the live HASHI runtime is denied: "
            f"operation={operation}; target={rendered}. Use an independent "
            "workzone/development environment or the supported HASHI lifecycle command."
        ),
    )
