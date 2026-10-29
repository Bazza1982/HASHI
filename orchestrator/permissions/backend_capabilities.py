"""Conservative projections of observed backend controls.

This pure function does not authenticate the producer, discover execution
paths, enforce permissions, or change backend selection. A trusted PAO/runtime
must supply the complete active-path inventory, the current binding and a
verified service observation. Until those inputs exist, this module must not be
used to claim that L1 or L2 is active.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
import math
from typing import Iterable

from orchestrator.permissions.models import PermissionLevel


@dataclass(frozen=True, slots=True)
class BackendPermissionStatus:
    """Configured level and conservative coverage, not an enforcement receipt."""

    requested_level: PermissionLevel
    backend_id: str
    state: str

    def __post_init__(self) -> None:
        if (not isinstance(self.requested_level, PermissionLevel)
                or self.requested_level is PermissionLevel.L0
                or not _text(self.backend_id) or self.state not in {"unmanaged", "unavailable"}):
            raise ValueError("invalid current-backend permission status")


CONTROLS = ("files", "scripts", "network", "credentials", "devices", "approval", "cleanup")
_STATES = frozenset({"enforced", "partial", "unmanaged", "unavailable", "unknown"})
_LAYERS = frozenset({"L1", "L2"})


def _text(value: object) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 512
            and not any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                        for char in value))


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


@dataclass(frozen=True, slots=True)
class BackendBinding:
    instance_id: str
    backend_id: str
    backend_version: str
    executor_profile: str
    executor_version: str
    service_generation: str
    capability_revision: str

    def __post_init__(self) -> None:
        if any(not _text(getattr(self, name)) for name in self.__dataclass_fields__):
            raise ValueError("a complete observed backend binding is required")


@dataclass(frozen=True, slots=True)
class ActivePath:
    path_id: str
    controls: tuple[str, ...]

    def __post_init__(self) -> None:
        if (not _text(self.path_id) or not isinstance(self.controls, tuple) or not self.controls
                or any(not isinstance(name, str) or name not in CONTROLS for name in self.controls)
                or len(set(self.controls)) != len(self.controls)):
            raise ValueError("invalid active-path inventory")


@dataclass(frozen=True, slots=True)
class ControlFinding:
    path_id: str
    control: str
    state: str
    layer: str | None
    reason: str
    dependencies: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if (not _text(self.path_id) or self.control not in CONTROLS
                or not isinstance(self.state, str) or self.state not in _STATES
                or not _text(self.reason)
                or (self.layer is not None and (not isinstance(self.layer, str) or self.layer not in _LAYERS))
                or (self.state == "enforced" and self.layer is None)
                or (self.state == "unmanaged" and self.layer is not None)
                or not isinstance(self.dependencies, tuple) or len(self.dependencies) > 448):
            raise ValueError("invalid control observation")
        for dependency in self.dependencies:
            if (not isinstance(dependency, tuple) or len(dependency) != 2
                    or not _text(dependency[0]) or dependency[1] not in CONTROLS):
                raise ValueError("invalid control dependency")


@dataclass(frozen=True, slots=True)
class ServiceObservation:
    binding: BackendBinding
    observed_at: float
    expires_at: float
    healthy: bool
    findings: tuple[ControlFinding, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.binding, BackendBinding) or type(self.healthy) is not bool:
            raise ValueError("invalid service observation")
        for value in (self.observed_at, self.expires_at):
            if not _finite_number(value):
                raise ValueError("invalid observation time")
        if (self.expires_at <= self.observed_at or not isinstance(self.findings, tuple)
                or len(self.findings) > 448):
            raise ValueError("invalid observation expiry or findings")
        if any(not isinstance(finding, ControlFinding) for finding in self.findings):
            raise ValueError("invalid control finding")
        keys = [(finding.path_id, finding.control) for finding in self.findings]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate control finding")


@dataclass(frozen=True, slots=True)
class ActualControl:
    state: str
    layer: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class BackendControlReport:
    requested_level: PermissionLevel
    binding: BackendBinding
    controls: tuple[tuple[str, ActualControl], ...]

    def control(self, name: str) -> ActualControl:
        for control, value in self.controls:
            if control == name:
                return value
        raise KeyError(name)


def project_controls(
    requested_level: PermissionLevel,
    binding: BackendBinding,
    active_paths: Iterable[ActivePath],
    *,
    inventory_complete: bool,
    observation: ServiceObservation | None,
    now: float,
    max_age_seconds: float = 30.0,
) -> BackendControlReport:
    """Project only fresh evidence for the exact active backend and paths.

    The inventory and observation must be supplied by trusted runtime owners,
    not model arguments or public request metadata. Missing evidence is unknown.
    Nothing here authorizes execution or prevents a user selecting a backend.
    """
    if not isinstance(requested_level, PermissionLevel) or not isinstance(binding, BackendBinding):
        raise ValueError("configured level and current binding are required")
    if type(inventory_complete) is not bool:
        raise ValueError("inventory completeness must be explicit")
    for value in (now, max_age_seconds):
        if not _finite_number(value):
            raise ValueError("invalid observation clock")
    if not 0 < max_age_seconds <= 300:
        raise ValueError("invalid observation maximum age")
    paths = tuple(islice(active_paths, 65))
    if len(paths) > 64:
        raise ValueError("too many active paths")
    if any(not isinstance(path, ActivePath) for path in paths):
        raise ValueError("invalid active path")
    if len({path.path_id for path in paths}) != len(paths):
        raise ValueError("duplicate active path")
    expected = {(path.path_id, control) for path in paths for control in path.controls}
    unknown = ActualControl("unknown", None, "Current control evidence is not available.")

    def finish(values: dict[str, ActualControl]) -> BackendControlReport:
        return BackendControlReport(requested_level, binding, tuple((name, values[name]) for name in CONTROLS))

    if (requested_level is PermissionLevel.L0
            or not inventory_complete or not paths or observation is None
            or not isinstance(observation, ServiceObservation) or observation.binding != binding
            or now < observation.observed_at or now >= observation.expires_at
            or now - observation.observed_at > max_age_seconds):
        return finish({name: unknown for name in CONTROLS})

    findings = {(item.path_id, item.control): item for item in observation.findings}
    cache: dict[tuple[str, str], ActualControl] = {}

    def evaluate(key: tuple[str, str], stack: frozenset[tuple[str, str]]) -> ActualControl:
        if key in cache:
            return cache[key]
        if key not in expected or key in stack:
            return unknown
        finding = findings.get(key)
        if finding is None:
            return unknown
        if not observation.healthy and finding.state not in {"unmanaged", "unavailable"}:
            result = ActualControl("unavailable", None, "The permission service is not healthy.")
        else:
            result = ActualControl(finding.state, finding.layer, finding.reason)
        if result.state == "enforced":
            dependencies = [
                (dependency, evaluate(dependency, stack | {key}))
                for dependency in finding.dependencies
            ]
            missing = min((pair for pair in dependencies if pair[1].state == "unknown"),
                          key=lambda pair: pair[0], default=None)
            unavailable = min((pair for pair in dependencies if pair[1].state == "unavailable"),
                              key=lambda pair: pair[0], default=None)
            incomplete = min((pair for pair in dependencies if pair[1].state != "enforced"),
                             key=lambda pair: pair[0], default=None)
            lower = min((pair for pair in dependencies
                         if result.layer == "L2" and pair[1].layer != "L2"),
                        key=lambda pair: pair[0], default=None)
            if missing:
                result = ActualControl("unknown", None, f"Unverified dependency: {missing[0][0]} / {missing[0][1]}.")
            elif unavailable:
                result = ActualControl("unavailable", None,
                                       f"Unavailable dependency: {unavailable[0][0]} / {unavailable[0][1]}.")
            elif incomplete or lower:
                issue = incomplete or lower
                layer = "L1" if result.layer == "L1" or issue[1].layer == "L1" else None
                result = ActualControl("partial", layer, f"Incomplete dependency: {issue[0][0]} / {issue[0][1]}.")
            if result.state == "enforced" and requested_level is PermissionLevel.L2 and result.layer != "L2":
                result = ActualControl("partial", result.layer, "This path is not enforced at L2.")
        cache[key] = result
        return result

    projected = {}
    for control in CONTROLS:
        values = [(key[0], evaluate(key, frozenset())) for key in sorted(expected) if key[1] == control]
        if not values:
            projected[control] = unknown
            continue
        unverified = [(path, value) for path, value in values if value.state == "unknown"]
        if unverified:
            reasons = "; ".join(f"{path}: {value.reason}" for path, value in unverified)
            projected[control] = ActualControl("unknown", None, reasons)
            continue
        states = {value.state for _, value in values}
        if states == {"enforced"}:
            layer = "L1" if any(value.layer == "L1" for _, value in values) else "L2"
            projected[control] = ActualControl("enforced", layer, "All observed active paths are enforced.")
            continue
        reasons = "; ".join(f"{path}: {value.reason}" for path, value in values if value.state != "enforced")
        if len(states) == 1 and "partial" not in states:
            layers = {value.layer for _, value in values}
            projected[control] = ActualControl(values[0][1].state, layers.pop() if len(layers) == 1 else None, reasons)
        else:
            projected[control] = ActualControl("partial", None, reasons)
    return finish(projected)


def changed_controls(before: BackendControlReport, after: BackendControlReport) -> tuple[str, ...]:
    """Return only material coverage changes; backend selection remains external."""
    if not isinstance(before, BackendControlReport) or not isinstance(after, BackendControlReport):
        raise ValueError("two backend reports are required")
    return tuple(name for name in CONTROLS if before.control(name) != after.control(name))
