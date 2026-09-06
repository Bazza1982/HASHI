"""Stable Core broker for isolated browser and computer-control workers."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from uuid import uuid4

from orchestrator.file_permissions import tighten_fd_permissions
from orchestrator.service_endpoints import (
    ServiceEndpoint,
    discover_worker_callback_hosts,
    normalize_instance_id,
)


logger = logging.getLogger(__name__)


CAPABILITY_PROTOCOL_VERSION = 1
CAPABILITY_REGISTRATION_SCHEMA_VERSION = 1
DEFAULT_REGISTRATION_TTL_SECONDS = 90.0
MAX_REGISTRATION_TTL_SECONDS = 300.0
DEFAULT_LEASE_TTL_SECONDS = 30.0
MAX_LEASE_TTL_SECONDS = 120.0
MAX_ACTION_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_HEALTH_RESPONSE_BYTES = 1024 * 1024
CAPABILITY_KINDS = frozenset({"browser_control", "computer_control"})
MUTATING_ACTIONS = frozenset(
    {
        "click",
        "drag",
        "fill",
        "hover",
        "key",
        "media_play",
        "mouse_move",
        "navigation",
        "open_play_verify",
        "react",
        "reset_input_state",
        "scroll",
        "select",
        "session",
        "session_close",
        "type",
        "type_text",
        "upload",
        "window_close",
        "window_focus",
    }
)


class CapabilityBrokerError(RuntimeError):
    """A capability registration, route, authorization, or action failed."""


class CapabilityLeaseConflict(CapabilityBrokerError):
    """A different task currently owns the device/session write lease."""


def _text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise CapabilityBrokerError(f"{field} is required")
    return result


def _endpoint(value: Any) -> str:
    endpoint = _text(value, "negotiated_endpoint").rstrip("/")
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port is None:
        raise CapabilityBrokerError(
            "negotiated_endpoint must be an HTTP(S) origin with an explicit port"
        )
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise CapabilityBrokerError("negotiated_endpoint must not contain credentials or a path")
    if parsed.hostname in {"0.0.0.0", "::"}:
        raise CapabilityBrokerError("negotiated_endpoint must use a connectable host")
    return endpoint


@dataclass(frozen=True)
class CapabilityRegistration:
    capability_id: str
    capability_kind: str
    instance_id: str
    device_id: str
    user_session_id: str
    platform: str
    transport_kind: str
    negotiated_endpoint: str
    protocol_version: int
    supported_actions: tuple[str, ...]
    worker_pid: int
    worker_generation: str
    authorization_key_id: str
    registered_at: float
    expires_at: float

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> "CapabilityRegistration":
        current = time.time() if now is None else float(now)
        kind = _text(value.get("capability_kind"), "capability_kind").casefold()
        if kind not in CAPABILITY_KINDS:
            raise CapabilityBrokerError(f"unsupported capability kind: {kind}")
        protocol = int(value.get("protocol_version") or 0)
        if protocol != CAPABILITY_PROTOCOL_VERSION:
            raise CapabilityBrokerError(
                f"capability protocol mismatch: received={protocol} required={CAPABILITY_PROTOCOL_VERSION}"
            )
        actions = tuple(
            sorted(
                {
                    str(action).strip().casefold()
                    for action in value.get("supported_actions", ())
                    if str(action).strip()
                }
            )
        )
        if not actions:
            raise CapabilityBrokerError("supported_actions must not be empty")
        worker = value.get("worker_pid_and_generation") or {}
        if not isinstance(worker, Mapping):
            raise CapabilityBrokerError("worker_pid_and_generation must be an object")
        pid = int(worker.get("pid") or 0)
        if pid <= 0:
            raise CapabilityBrokerError("worker pid must be positive")
        health = value.get("health_and_expiry") or {}
        if not isinstance(health, Mapping):
            raise CapabilityBrokerError("health_and_expiry must be an object")
        ttl = max(
            1.0,
            min(
                float(health.get("ttl_seconds") or DEFAULT_REGISTRATION_TTL_SECONDS),
                MAX_REGISTRATION_TTL_SECONDS,
            ),
        )
        return cls(
            capability_id=_text(value.get("capability_id"), "capability_id"),
            capability_kind=kind,
            instance_id=normalize_instance_id(value.get("instance_id")),
            device_id=_text(value.get("device_id"), "device_id"),
            user_session_id=_text(value.get("user_session_id"), "user_session_id"),
            platform=_text(value.get("platform"), "platform"),
            transport_kind=_text(value.get("transport_kind"), "transport_kind").casefold(),
            negotiated_endpoint=_endpoint(value.get("negotiated_endpoint")),
            protocol_version=protocol,
            supported_actions=actions,
            worker_pid=pid,
            worker_generation=_text(worker.get("generation"), "worker generation"),
            authorization_key_id=_text(
                value.get("authorization_key_id"),
                "authorization_key_id",
            ),
            registered_at=current,
            expires_at=current + ttl,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ControlLease:
    lease_id: str
    capability_id: str
    capability_kind: str
    instance_id: str
    device_id: str
    user_session_id: str
    window_id: str | None
    agent_id: str
    task_id: str
    issued_at: float
    expires_at: float

    @property
    def resource_key(self) -> tuple[str, str, str, str | None]:
        # Mouse, keyboard and foreground focus are shared by the whole desktop
        # session.  A window label is audit context, not an independent lock.
        return (
            self.instance_id,
            self.device_id,
            self.user_session_id,
            None,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


Transport = Callable[
    [CapabilityRegistration, str, dict[str, Any], float],
    Awaitable[dict[str, Any]],
]
RegistrationProbe = Callable[
    [CapabilityRegistration, str],
    Mapping[str, Any],
]


class CapabilityBroker:
    """Own discovery, authorization, audit, and device write leases in Core."""

    def __init__(self, kernel: Any) -> None:
        self.kernel = kernel
        self._records: dict[str, tuple[CapabilityRegistration, str]] = {}
        self._leases: dict[str, ControlLease] = {}
        self._resource_leases: dict[tuple[str, str, str, str | None], str] = {}
        self._bootstrap_token = ""
        self._registration_url = ""
        self._transport: Transport = self._http_transport
        self._registration_probe: RegistrationProbe = self._http_health_probe
        self._lock = threading.RLock()
        self._audit_lock = threading.Lock()

    @property
    def instance_id(self) -> str:
        configured = getattr(getattr(self.kernel, "global_cfg", None), "instance_id", None)
        path_identity = getattr(getattr(self.kernel, "paths", None), "instance_id", None)
        return normalize_instance_id(configured or path_identity or "HASHI")

    @property
    def bootstrap_path(self) -> Path:
        return Path(self.kernel.paths.bridge_home) / "state" / "device_control" / "bootstrap.json"

    @property
    def audit_path(self) -> Path:
        return Path(self.kernel.paths.bridge_home) / "state" / "device_control" / "audit.jsonl"

    def set_transport_for_testing(self, transport: Transport) -> None:
        self._transport = transport

    def set_registration_probe_for_testing(
        self,
        probe: RegistrationProbe,
    ) -> None:
        self._registration_probe = probe

    def start(self, workbench_endpoint: ServiceEndpoint) -> dict[str, Any]:
        if workbench_endpoint.instance_id != self.instance_id:
            raise CapabilityBrokerError(
                "capability bootstrap rejected a cross-instance Workbench endpoint"
            )
        self._bootstrap_token = secrets.token_urlsafe(48)
        self._registration_url = (
            workbench_endpoint.base_url + "/api/device-capabilities/register"
        )
        payload = {
            "schema_version": CAPABILITY_REGISTRATION_SCHEMA_VERSION,
            "protocol_version": CAPABILITY_PROTOCOL_VERSION,
            "instance_id": self.instance_id,
            "registration_url": self._registration_url,
            "heartbeat_url": (
                workbench_endpoint.base_url + "/api/device-capabilities/heartbeat"
            ),
            "worker_callback_hosts": list(discover_worker_callback_hosts()),
            "bootstrap_token": self._bootstrap_token,
            "issued_at": time.time(),
        }
        self._write_private_json(self.bootstrap_path, payload)
        return {key: value for key, value in payload.items() if key != "bootstrap_token"}

    def stop(self) -> None:
        with self._lock:
            for lease_id in list(self._leases):
                self.release_lease(lease_id, reason="core-stopped")
            self._records.clear()
            self._leases.clear()
            self._resource_leases.clear()
            self._bootstrap_token = ""
            self._registration_url = ""
        try:
            self.bootstrap_path.unlink(missing_ok=True)
        except OSError:
            pass

    def register(
        self,
        payload: Mapping[str, Any],
        *,
        bootstrap_token: str,
    ) -> CapabilityRegistration:
        if not self._bootstrap_token or not hmac.compare_digest(
            self._bootstrap_token,
            str(bootstrap_token or ""),
        ):
            raise CapabilityBrokerError("capability bootstrap authentication failed")
        registration = CapabilityRegistration.from_mapping(payload)
        if registration.instance_id != self.instance_id:
            raise CapabilityBrokerError(
                "cross-instance capability registration rejected: "
                f"expected={self.instance_id} received={registration.instance_id}"
            )
        worker_token = _text(payload.get("worker_token"), "worker_token")
        if len(worker_token) < 32:
            raise CapabilityBrokerError("worker_token is too short")
        expected_key_id = hashlib.sha256(worker_token.encode("utf-8")).hexdigest()[:24]
        if not hmac.compare_digest(
            expected_key_id,
            registration.authorization_key_id,
        ):
            raise CapabilityBrokerError("authorization_key_id does not match worker token")
        try:
            health = self._registration_probe(registration, worker_token)
        except Exception as exc:
            raise CapabilityBrokerError(
                f"capability endpoint health handshake failed: {exc}"
            ) from exc
        self._validate_health(registration, health)
        with self._lock:
            if registration.capability_id in self._records:
                replaced_leases = [
                    lease.lease_id
                    for lease in self._leases.values()
                    if lease.capability_id == registration.capability_id
                ]
                for lease_id in replaced_leases:
                    self.release_lease(lease_id, reason="capability-replaced")
            self._records[registration.capability_id] = (registration, worker_token)
        self._audit(
            "capability_registered",
            capability_id=registration.capability_id,
            capability_kind=registration.capability_kind,
            device_id=registration.device_id,
            user_session_id=registration.user_session_id,
        )
        return registration

    def heartbeat(
        self,
        capability_id: str,
        *,
        worker_token: str,
        identity: Mapping[str, Any],
        ttl_seconds: float = DEFAULT_REGISTRATION_TTL_SECONDS,
    ) -> CapabilityRegistration:
        with self._lock:
            registration, stored_token = self._record(capability_id)
            if not hmac.compare_digest(stored_token, str(worker_token or "")):
                raise CapabilityBrokerError("capability heartbeat authentication failed")
            self._verify_identity(registration, identity)
        try:
            health = self._registration_probe(registration, stored_token)
            self._validate_health(registration, health)
        except Exception as exc:
            removed = False
            with self._lock:
                current = self._records.get(registration.capability_id)
                if current == (registration, stored_token):
                    self._records.pop(registration.capability_id, None)
                    lease_ids = [
                        lease.lease_id
                        for lease in self._leases.values()
                        if lease.capability_id == registration.capability_id
                    ]
                    for lease_id in lease_ids:
                        self.release_lease(lease_id, reason="capability-unhealthy")
                    removed = True
            if removed:
                self._audit(
                    "capability_unhealthy",
                    capability_id=registration.capability_id,
                    capability_kind=registration.capability_kind,
                    error_type=type(exc).__name__,
                )
            raise CapabilityBrokerError(
                f"capability heartbeat health check failed: {exc}"
            ) from exc
        with self._lock:
            current = self._records.get(registration.capability_id)
            if current != (registration, stored_token):
                raise CapabilityBrokerError(
                    "capability heartbeat became stale during health validation"
                )
            ttl = max(1.0, min(float(ttl_seconds), MAX_REGISTRATION_TTL_SECONDS))
            refreshed = CapabilityRegistration(
                **{
                    **registration.to_dict(),
                    "expires_at": time.time() + ttl,
                }
            )
            self._records[registration.capability_id] = (refreshed, stored_token)
            return refreshed

    def _validate_health(
        self,
        registration: CapabilityRegistration,
        health: Mapping[str, Any],
    ) -> None:
        if health.get("ok") is not True:
            raise CapabilityBrokerError("capability endpoint is not healthy")
        if int(health.get("protocol_version") or 0) != CAPABILITY_PROTOCOL_VERSION:
            raise CapabilityBrokerError("capability endpoint protocol handshake failed")
        self._verify_identity(registration, health.get("identity"))
        health_kind = str(health.get("capability_kind") or "").strip().casefold()
        if health_kind != registration.capability_kind:
            raise CapabilityBrokerError("capability endpoint kind handshake failed")
        health_actions = {
            str(action).strip().casefold()
            for action in health.get("supported_actions", ())
            if str(action).strip()
        }
        if health_actions != set(registration.supported_actions):
            raise CapabilityBrokerError("capability endpoint action handshake failed")

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._prune()
            return {
                "protocol_version": CAPABILITY_PROTOCOL_VERSION,
                "instance_id": self.instance_id,
                "capabilities": [
                    registration.to_dict()
                    for registration, _token in sorted(
                        self._records.values(),
                        key=lambda item: item[0].capability_id,
                    )
                ],
                "leases": [
                    lease.to_dict()
                    for lease in sorted(
                        self._leases.values(),
                        key=lambda item: item.lease_id,
                    )
                ],
            }

    def has_capability(self, capability_kind: str) -> bool:
        with self._lock:
            self._prune()
            kind = str(capability_kind or "").strip().casefold()
            return any(
                record.capability_kind == kind
                for record, _token in self._records.values()
            )

    def acquire_lease(
        self,
        registration: CapabilityRegistration,
        *,
        agent_id: str,
        task_id: str,
        window_id: str | None = None,
        ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
    ) -> ControlLease:
        with self._lock:
            self._prune()
            issued = time.time()
            ttl = max(0.1, min(float(ttl_seconds), MAX_LEASE_TTL_SECONDS))
            resource = (
                registration.instance_id,
                registration.device_id,
                registration.user_session_id,
                None,
            )
            existing_id = self._resource_leases.get(resource)
            existing = self._leases.get(existing_id or "")
            if existing is not None:
                if (
                    existing.agent_id == str(agent_id)
                    and existing.task_id == str(task_id)
                    and existing.capability_id == registration.capability_id
                ):
                    return existing
                if (
                    existing.agent_id == str(agent_id)
                    and existing.task_id == str(task_id)
                ):
                    raise CapabilityLeaseConflict(
                        "task already holds another capability lease; explicit handoff is required"
                    )
                raise CapabilityLeaseConflict(
                    "device/session control lease is already held by another task"
                )
            lease = ControlLease(
                lease_id="lease-" + uuid4().hex,
                capability_id=registration.capability_id,
                capability_kind=registration.capability_kind,
                instance_id=registration.instance_id,
                device_id=registration.device_id,
                user_session_id=registration.user_session_id,
                window_id=(str(window_id) if window_id is not None else None),
                agent_id=_text(agent_id, "agent_id"),
                task_id=_text(task_id, "task_id"),
                issued_at=issued,
                expires_at=issued + ttl,
            )
            self._leases[lease.lease_id] = lease
            self._resource_leases[resource] = lease.lease_id
        self._audit("lease_acquired", **lease.to_dict())
        return lease

    def acquire_for_task(
        self,
        capability_kind: str,
        *,
        action: str | None,
        agent_id: str,
        task_id: str,
        window_id: str | None = None,
        ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
    ) -> ControlLease:
        registration, _token = self._select(capability_kind, action=action)
        return self.acquire_lease(
            registration,
            agent_id=agent_id,
            task_id=task_id,
            window_id=window_id,
            ttl_seconds=ttl_seconds,
        )

    def release_lease(self, lease_id: str, *, reason: str = "completed") -> bool:
        with self._lock:
            lease = self._leases.pop(str(lease_id), None)
            if lease is None:
                return False
            registration_record = self._records.get(lease.capability_id)
            if self._resource_leases.get(lease.resource_key) == lease.lease_id:
                self._resource_leases.pop(lease.resource_key, None)
        self._audit("lease_released", lease_id=lease.lease_id, reason=str(reason))
        if reason in {
            "action-failed",
            "agent-stopped",
            "cancelled",
            "core-stopped",
            "expired",
            "function-worker-exited",
            "worker-cancelled",
        } and registration_record is not None:
            registration, worker_token = registration_record
            self._schedule_cleanup(
                registration,
                worker_token,
                lease=lease,
                reason=reason,
            )
        return True

    def release_task_lease(
        self,
        lease_id: str,
        *,
        agent_id: str,
        task_id: str,
        reason: str = "task-released",
    ) -> bool:
        with self._lock:
            self._prune()
            lease = self._leases.get(str(lease_id))
            if lease is None:
                return False
            if lease.agent_id != str(agent_id) or lease.task_id != str(task_id):
                raise CapabilityBrokerError(
                    "lease release owner does not match the active task"
                )
        return self.release_lease(lease.lease_id, reason=reason)

    def cancel_task(self, *, agent_id: str, task_id: str, reason: str = "cancelled") -> int:
        with self._lock:
            matches = [
                lease.lease_id
                for lease in self._leases.values()
                if lease.agent_id == str(agent_id) and lease.task_id == str(task_id)
            ]
            for lease_id in matches:
                self.release_lease(lease_id, reason=reason)
        return len(matches)

    def cancel_agent(self, agent_id: str, *, reason: str = "agent-stopped") -> int:
        with self._lock:
            matches = [
                lease.lease_id
                for lease in self._leases.values()
                if lease.agent_id == str(agent_id)
            ]
            for lease_id in matches:
                self.release_lease(lease_id, reason=reason)
        return len(matches)

    def handoff(
        self,
        source_lease_id: str,
        *,
        target_capability_kind: str,
        agent_id: str,
        task_id: str,
        window_id: str | None = None,
    ) -> ControlLease:
        with self._lock:
            self._prune()
            source = self._leases.get(str(source_lease_id))
            if source is None:
                raise CapabilityBrokerError("source lease is unavailable for handoff")
            if source.agent_id != str(agent_id) or source.task_id != str(task_id):
                raise CapabilityBrokerError("handoff owner does not match source lease")
            target, _token = self._select(target_capability_kind, action=None)
            if (
                target.instance_id != source.instance_id
                or target.device_id != source.device_id
                or target.user_session_id != source.user_session_id
            ):
                raise CapabilityBrokerError("handoff target does not match source device session")
            issued = time.time()
            lease = ControlLease(
                lease_id="lease-" + uuid4().hex,
                capability_id=target.capability_id,
                capability_kind=target.capability_kind,
                instance_id=target.instance_id,
                device_id=target.device_id,
                user_session_id=target.user_session_id,
                window_id=(str(window_id) if window_id is not None else None),
                agent_id=_text(agent_id, "agent_id"),
                task_id=_text(task_id, "task_id"),
                issued_at=issued,
                expires_at=issued + DEFAULT_LEASE_TTL_SECONDS,
            )
            self._leases.pop(source.lease_id, None)
            self._leases[lease.lease_id] = lease
            self._resource_leases[source.resource_key] = lease.lease_id
        self._audit(
            "lease_handoff",
            source_capability_id=source.capability_id,
            target_capability_id=target.capability_id,
            source_lease_id=source.lease_id,
            target_lease_id=lease.lease_id,
            agent_id=str(agent_id),
            task_id=str(task_id),
        )
        return lease

    async def invoke(
        self,
        capability_kind: str,
        action: str,
        args: Mapping[str, Any] | None,
        *,
        agent_id: str,
        task_id: str,
        request_id: str | None = None,
        authorization: str = "tool_registry",
        timeout_seconds: float = 60.0,
        lease_id: str | None = None,
    ) -> Any:
        registration, worker_token = self._select(capability_kind, action=action)
        normalized_action = _text(action, "action").casefold()
        if authorization not in {
            "tool_registry",
            "policy",
            "explicit_user_authorization",
        }:
            raise CapabilityBrokerError("device-control authorization is missing")
        lease = None
        release_after_action = False
        # The current browser adapter may navigate while resolving any action
        # that carries a URL, including apparent reads.  Until the provider can
        # prove a side-effect-free observation contract, serialize every
        # browser action behind the same short control lease.
        if (
            registration.capability_kind == "browser_control"
            or normalized_action in MUTATING_ACTIONS
        ):
            if lease_id:
                lease = self._lease_for_action(
                    str(lease_id),
                    registration=registration,
                    agent_id=agent_id,
                    task_id=task_id,
                )
            else:
                lease = self.acquire_lease(
                    registration,
                    agent_id=agent_id,
                    task_id=task_id,
                    window_id=(args or {}).get("window_id"),
                )
                release_after_action = True
        correlation_id = str(request_id or "cap-" + uuid4().hex)
        payload = {
            "protocol_version": CAPABILITY_PROTOCOL_VERSION,
            "request_id": correlation_id,
            "identity": {
                "instance_id": self.instance_id,
                "capability_id": registration.capability_id,
                "device_id": registration.device_id,
                "user_session_id": registration.user_session_id,
            },
            "agent_id": _text(agent_id, "agent_id"),
            "task_id": _text(task_id, "task_id"),
            "action": normalized_action,
            "args": dict(args or {}),
            "lease": lease.to_dict() if lease is not None else None,
        }
        started = time.perf_counter()
        action_succeeded = False
        try:
            response = await self._transport(
                registration,
                worker_token,
                payload,
                max(0.1, float(timeout_seconds)),
            )
            self._verify_identity(
                registration,
                response.get("identity") if isinstance(response, Mapping) else None,
            )
            if not response.get("ok"):
                raise CapabilityBrokerError(
                    str(response.get("error") or "capability Worker rejected action")
                )
            self._audit(
                "capability_action",
                capability_id=registration.capability_id,
                capability_kind=registration.capability_kind,
                action=normalized_action,
                request_id=correlation_id,
                agent_id=str(agent_id),
                task_id=str(task_id),
                argument_names=sorted(str(key) for key in (args or {})),
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                ok=True,
            )
            action_succeeded = True
            return response.get("result")
        except Exception as exc:
            self._audit(
                "capability_action",
                capability_id=registration.capability_id,
                capability_kind=registration.capability_kind,
                action=normalized_action,
                request_id=correlation_id,
                agent_id=str(agent_id),
                task_id=str(task_id),
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                ok=False,
                error_type=type(exc).__name__,
            )
            raise
        finally:
            if lease is not None and release_after_action:
                self.release_lease(
                    lease.lease_id,
                    reason=("action-finished" if action_succeeded else "action-failed"),
                )

    def _lease_for_action(
        self,
        lease_id: str,
        *,
        registration: CapabilityRegistration,
        agent_id: str,
        task_id: str,
    ) -> ControlLease:
        with self._lock:
            self._prune()
            lease = self._leases.get(str(lease_id))
            if lease is None:
                raise CapabilityBrokerError("capability action lease is unavailable")
            if (
                lease.capability_id != registration.capability_id
                or lease.instance_id != registration.instance_id
                or lease.device_id != registration.device_id
                or lease.user_session_id != registration.user_session_id
            ):
                raise CapabilityBrokerError(
                    "capability action lease does not match the selected worker"
                )
            if lease.agent_id != str(agent_id) or lease.task_id != str(task_id):
                raise CapabilityBrokerError(
                    "capability action lease owner does not match the active task"
                )
            return lease

    def _record(self, capability_id: str) -> tuple[CapabilityRegistration, str]:
        with self._lock:
            self._prune()
            record = self._records.get(str(capability_id))
            if record is None:
                raise CapabilityBrokerError(f"capability is unavailable: {capability_id}")
            return record

    def _select(
        self,
        capability_kind: str,
        *,
        action: str | None,
    ) -> tuple[CapabilityRegistration, str]:
        with self._lock:
            self._prune()
            kind = _text(capability_kind, "capability_kind").casefold()
            normalized_action = str(action or "").strip().casefold()
            candidates = [
                record
                for record in self._records.values()
                if record[0].capability_kind == kind
                and (
                    not normalized_action
                    or normalized_action in record[0].supported_actions
                )
            ]
            if not candidates:
                suffix = f" for action {normalized_action!r}" if normalized_action else ""
                raise CapabilityBrokerError(
                    f"capability is unavailable: {kind}{suffix}"
                )
            return sorted(
                candidates,
                key=lambda item: item[0].registered_at,
                reverse=True,
            )[0]

    def _verify_identity(
        self,
        registration: CapabilityRegistration,
        identity: Mapping[str, Any] | None,
    ) -> None:
        if not isinstance(identity, Mapping):
            raise CapabilityBrokerError("capability response identity is missing")
        expected = {
            "instance_id": registration.instance_id,
            "capability_id": registration.capability_id,
            "device_id": registration.device_id,
            "user_session_id": registration.user_session_id,
        }
        received = {
            key: str(identity.get(key) or "").strip()
            for key in expected
        }
        received["instance_id"] = received["instance_id"].upper()
        if received != expected:
            raise CapabilityBrokerError(
                f"capability identity mismatch: expected={expected} received={received}"
            )

    def _prune(self) -> None:
        now = time.time()
        expired_capabilities = [
            capability_id
            for capability_id, (registration, _token) in self._records.items()
            if registration.expires_at <= now
        ]
        for capability_id in expired_capabilities:
            self._records.pop(capability_id, None)
        expired_leases = [
            lease_id
            for lease_id, lease in self._leases.items()
            if lease.expires_at <= now or lease.capability_id in expired_capabilities
        ]
        for lease_id in expired_leases:
            self.release_lease(lease_id, reason="expired")

    async def _http_transport(
        self,
        registration: CapabilityRegistration,
        worker_token: str,
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        url = registration.negotiated_endpoint + "/action"

        def request() -> dict[str, Any]:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib_request.Request(
                url,
                data=encoded,
                headers={
                    "Authorization": f"Bearer {worker_token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib_request.urlopen(req, timeout=timeout_seconds) as response:
                    raw = response.read(MAX_ACTION_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_ACTION_RESPONSE_BYTES:
                        raise CapabilityBrokerError(
                            "capability Worker response exceeds the size limit"
                        )
                    result = json.loads(raw.decode("utf-8"))
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise CapabilityBrokerError(
                    f"capability Worker HTTP {exc.code}: {detail[:1000]}"
                ) from exc
            except (URLError, OSError, json.JSONDecodeError) as exc:
                raise CapabilityBrokerError(f"capability Worker transport failed: {exc}") from exc
            if not isinstance(result, dict):
                raise CapabilityBrokerError("capability Worker response must be an object")
            return result

        return await asyncio.to_thread(request)

    def _schedule_cleanup(
        self,
        registration: CapabilityRegistration,
        worker_token: str,
        *,
        lease: ControlLease,
        reason: str,
    ) -> None:
        thread = threading.Thread(
            target=self._best_effort_cleanup,
            args=(registration, worker_token, lease, str(reason)),
            name=f"hashi-capability-cleanup-{lease.lease_id[-8:]}",
            daemon=True,
        )
        thread.start()

    def _best_effort_cleanup(
        self,
        registration: CapabilityRegistration,
        worker_token: str,
        lease: ControlLease,
        reason: str,
    ) -> None:
        payload = {
            "protocol_version": CAPABILITY_PROTOCOL_VERSION,
            "request_id": "cleanup-" + uuid4().hex,
            "identity": {
                "instance_id": registration.instance_id,
                "capability_id": registration.capability_id,
                "device_id": registration.device_id,
                "user_session_id": registration.user_session_id,
            },
            "agent_id": lease.agent_id,
            "task_id": lease.task_id,
            "reason": str(reason),
        }
        req = urllib_request.Request(
            registration.negotiated_endpoint + "/cleanup",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {worker_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        ok = False
        try:
            with urllib_request.urlopen(req, timeout=1.0) as response:
                raw = response.read(MAX_HEALTH_RESPONSE_BYTES + 1)
            if len(raw) <= MAX_HEALTH_RESPONSE_BYTES:
                result = json.loads(raw.decode("utf-8"))
                self._verify_identity(registration, result.get("identity"))
                ok = result.get("ok") is True
        except Exception:
            ok = False
        self._audit(
            "capability_cleanup",
            capability_id=registration.capability_id,
            capability_kind=registration.capability_kind,
            lease_id=lease.lease_id,
            reason=str(reason),
            ok=ok,
        )

    def _http_health_probe(
        self,
        registration: CapabilityRegistration,
        worker_token: str,
    ) -> Mapping[str, Any]:
        url = registration.negotiated_endpoint + "/health"
        req = urllib_request.Request(
            url,
            headers={"Authorization": f"Bearer {worker_token}"},
            method="GET",
        )
        try:
            with urllib_request.urlopen(req, timeout=3.0) as response:
                raw = response.read(MAX_HEALTH_RESPONSE_BYTES + 1)
                if len(raw) > MAX_HEALTH_RESPONSE_BYTES:
                    raise CapabilityBrokerError(
                        "capability health response exceeds the size limit"
                    )
                payload = json.loads(raw.decode("utf-8"))
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
            raise CapabilityBrokerError(
                f"capability health request failed: {exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise CapabilityBrokerError("capability health response must be an object")
        return payload

    def _audit(self, event: str, **fields: Any) -> None:
        path = self.audit_path
        record = {
            "ts": time.time(),
            "event": str(event),
            "instance_id": self.instance_id,
            **fields,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_lock:
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(
                        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                    )
        except OSError as exc:
            logger.error(
                "Capability audit write failed for %s: %s",
                event,
                exc,
            )

    @staticmethod
    def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            tighten_fd_permissions(descriptor)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n"
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            Path(temporary).unlink(missing_ok=True)
            raise
