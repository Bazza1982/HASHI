"""Immutable PAO permission facts and versioned scope normalization.

This module does not authenticate a caller or enforce a resource boundary.
The PAO must construct execution identities from trusted runtime state. A file
scope is a request description; execution must bind the actual object again.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import ipaddress
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


class PermissionLevel(str, Enum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"


@dataclass(frozen=True, slots=True)
class PermissionSettings:
    requested_level: PermissionLevel = PermissionLevel.L0
    task_default_seconds: int = 4 * 60 * 60
    request_ttl_seconds: int = 24 * 60 * 60
    audit_retention_seconds: int = 90 * 24 * 60 * 60

    def __post_init__(self) -> None:
        if not isinstance(self.requested_level, PermissionLevel):
            raise ValueError("requested_level must be a PermissionLevel")
        for key in ("task_default_seconds", "request_ttl_seconds", "audit_retention_seconds"):
            value = getattr(self, key)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{key} must be a positive integer")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object] | None) -> PermissionSettings:
        # Absence is the legacy path. An explicit but invalid level must never
        # be interpreted as L0, which would silently weaken requested controls.
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ValueError("permission settings must be a mapping")
        allowed = {"requested_level", "task_default_seconds", "request_ttl_seconds", "audit_retention_seconds"}
        if set(value) - allowed:
            raise ValueError("unknown permission setting")
        raw_level = value.get("requested_level", PermissionLevel.L0.value)
        try:
            level = PermissionLevel(raw_level)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid requested permission level") from exc
        defaults = cls()
        durations = {}
        for key in ("task_default_seconds", "request_ttl_seconds", "audit_retention_seconds"):
            raw = value.get(key, getattr(defaults, key))
            if type(raw) is not int or raw <= 0:
                raise ValueError(f"{key} must be a positive integer")
            durations[key] = raw
        return cls(requested_level=level, **durations)


@dataclass(frozen=True, slots=True)
class PermissionSettingsSnapshot:
    agent_name: str
    settings: PermissionSettings
    revision: str


@dataclass(frozen=True, slots=True)
class PermissionSubject:
    """Trusted frontend read scope; never an approval or execution capability."""

    instance_id: str
    owner_id: str
    agent_id: str
    session_id: str
    context_generation: str

    def __post_init__(self) -> None:
        for name in ("instance_id", "owner_id", "agent_id", "session_id", "context_generation"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty trusted binding")


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Trusted runtime binding; creating one is not proof of caller identity."""

    instance_id: str
    owner_id: str
    agent_id: str
    task_id: str
    run_id: str
    session_id: str
    turn_id: str
    worker_generation: str
    context_generation: str
    execution_epoch: str
    backend_id: str
    executor_id: str
    policy_revision: int

    def __post_init__(self) -> None:
        for name in (
            "instance_id", "owner_id", "agent_id", "task_id", "run_id",
            "session_id", "turn_id", "worker_generation", "context_generation",
            "execution_epoch", "backend_id", "executor_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty trusted binding")
        if type(self.policy_revision) is not int or self.policy_revision < 0:
            raise ValueError("policy_revision must be a nonnegative integer")


@dataclass(frozen=True, slots=True)
class RuntimeExecutionBinding:
    """Recorded PAO claim facts; possession does not authenticate a caller."""

    context: ExecutionContext
    request_id: str
    fencing_token: int
    worker_id: str

    def __post_init__(self) -> None:
        if (not isinstance(self.context, ExecutionContext)
                or type(self.fencing_token) is not int or self.fencing_token <= 0):
            raise ValueError("a canonical execution and positive PAO fence are required")
        for value in (self.request_id, self.worker_id):
            if not isinstance(value, str) or not value.strip() or len(value) > 512:
                raise ValueError("a complete PAO runtime binding is required")


_SCOPE_ACTIONS = {
    "file": frozenset({"read", "list", "write", "create", "delete", "execute"}),
    "network": frozenset({"connect", "fetch", "upload", "send", "receive"}),
    "capability": frozenset({"use", "read", "write", "control", "execute"}),
}
_CAPABILITY = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}$")
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_NORMALIZATION_VERSION = 1


def _network_origin(resource: str) -> str:
    if not isinstance(resource, str) or not resource or any(ord(c) < 33 for c in resource):
        raise ValueError("network resource must be an explicit origin")
    try:
        parts = urlsplit(resource)
        scheme = parts.scheme.lower()
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid network origin") from exc
    if (
        scheme not in {"http", "https"} or not host or parts.username is not None
        or parts.password is not None or parts.path not in {"", "/"}
        or parts.query or parts.fragment or "@" in parts.netloc or "%" in host
    ):
        raise ValueError("network scope requires a scheme, host and port only")
    host = host.rstrip(".").lower()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("invalid network hostname") from exc
        labels = host.split(".")
        if len(host) > 253 or not labels or any(not _DNS_LABEL.fullmatch(label) for label in labels):
            raise ValueError("invalid network hostname")
        display_host = host
    else:
        display_host = f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
    port = port if port is not None else (443 if scheme == "https" else 80)
    if port <= 0 or port > 65535:
        raise ValueError("invalid network port")
    return f"{scheme}://{display_host}:{port}"


def _file_resource(resource: str, cwd: Path) -> str:
    if not isinstance(resource, str) or not resource or "\x00" in resource:
        raise ValueError("file resource must be a nonempty path")
    base = Path(cwd)
    if not base.is_absolute():
        raise ValueError("scope working directory must be absolute")
    path = Path(resource)
    if (path.drive or path.root) and not path.is_absolute():
        raise ValueError("drive-relative and root-relative paths are ambiguous")
    if not path.is_absolute():
        path = base / path
    try:
        return os.path.normcase(str(path.resolve(strict=False)))
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("cannot resolve file scope") from exc


@dataclass(frozen=True, slots=True)
class Scope:
    """A normalized resource and action set, not an execution authorization."""

    kind: str
    resource: str
    actions: tuple[str, ...]
    normalization_version: int = _NORMALIZATION_VERSION

    def __post_init__(self) -> None:
        if self.kind not in _SCOPE_ACTIONS:
            raise ValueError("unknown resource kind")
        if not isinstance(self.resource, str) or not self.resource:
            raise ValueError("resource is required")
        if self.normalization_version != _NORMALIZATION_VERSION or type(self.normalization_version) is not int:
            raise ValueError("unsupported scope normalization version")
        if not isinstance(self.actions, (tuple, list)) or not self.actions:
            raise ValueError("at least one explicit action is required")
        if any(not isinstance(action, str) or action not in _SCOPE_ACTIONS[self.kind] for action in self.actions):
            raise ValueError("unknown or wildcard action")
        object.__setattr__(self, "actions", tuple(sorted(set(self.actions))))
        if self.kind == "file":
            if not Path(self.resource).is_absolute() or _file_resource(self.resource, Path(self.resource).parent) != self.resource:
                raise ValueError("file scope must already be canonical")
        elif self.kind == "network":
            if _network_origin(self.resource) != self.resource:
                raise ValueError("network scope must already be canonical")
        elif not _CAPABILITY.fullmatch(self.resource):
            raise ValueError("invalid capability identifier")


def normalize_scope(kind: str, resource: str, actions: Iterable[str], *, cwd: Path) -> Scope:
    if kind == "file":
        canonical = _file_resource(resource, cwd)
    elif kind == "network":
        canonical = _network_origin(resource)
    elif kind == "capability":
        canonical = resource
    else:
        raise ValueError("unknown resource kind")
    if isinstance(actions, (str, bytes)):
        raise ValueError("actions must be a collection")
    try:
        action_tuple = tuple(actions)
    except TypeError as exc:
        raise ValueError("actions must be a collection") from exc
    return Scope(kind=kind, resource=canonical, actions=action_tuple)


def canonical_scope_set(scopes: Iterable[Scope]) -> tuple[Scope, ...]:
    values = tuple(scopes)
    if any(not isinstance(scope, Scope) for scope in values):
        raise ValueError("scope set contains an invalid scope")
    return tuple(sorted(set(values), key=lambda scope: (scope.kind, scope.resource, scope.actions)))


def scope_digest(scopes: Iterable[Scope]) -> str:
    normalized = canonical_scope_set(scopes)
    if not normalized:
        raise ValueError("an approval scope cannot be empty")
    payload = [
        {
            "normalization_version": scope.normalization_version,
            "kind": scope.kind,
            "resource": scope.resource,
            "actions": list(scope.actions),
        }
        for scope in normalized
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return sha256(encoded).hexdigest()


def scope_covers(granted: Scope, requested: Scope) -> bool:
    """Compare normalized requested scopes; callers must rebind actual resources."""
    if granted.kind != requested.kind or not set(requested.actions).issubset(granted.actions):
        return False
    if granted.normalization_version != requested.normalization_version:
        return False
    if granted.kind != "file":
        return granted.resource == requested.resource
    try:
        return os.path.commonpath((granted.resource, requested.resource)) == granted.resource
    except ValueError:  # Different Windows drives or incompatible absolute roots.
        return False
