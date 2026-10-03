from __future__ import annotations

import asyncio
import errno
import json
import socket
import threading
from datetime import datetime
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp

from orchestrator.runtime_common import QueuedRequest
from orchestrator.runtime_delivery import format_backend_error_for_user
from orchestrator import runtime_remote, ui_language
from orchestrator.agent_move.remote_client import (
    AgentMoveRemoteError,
    candidate_remote_base_urls,
    request_authenticated_json,
)
from orchestrator.service_endpoints import ServiceEndpointError
from remote.peer.base import normalize_instance_id
from remote.security.shared_token import load_shared_token


_TRANSFER_REDIRECT_ERROR_PREFIX = "HASHI_TRANSFER_REDIRECT_V1:"
_BRIDGE_HANDOFF_CLIENT_TIMEOUT_SECONDS = 3600
_MAX_REMOTE_HANDOFF_CANDIDATES = 8
_MAX_TRANSFER_FENCE_BYTES = 64 * 1024
_RETRYABLE_CONNECT_ERRNOS = {
    errno.ECONNREFUSED,
    errno.ECONNRESET,
    errno.EHOSTUNREACH,
    errno.ENETUNREACH,
    errno.ETIMEDOUT,
    101,  # Linux ENETUNREACH when this source runs on Windows
    104,  # Linux ECONNRESET when this source runs on Windows
    110,  # Linux ETIMEDOUT when this source runs on Windows
    111,  # Linux ECONNREFUSED when this source runs on Windows
    113,  # Linux EHOSTUNREACH when this source runs on Windows
    10051,  # WSAENETUNREACH
    10054,  # WSAECONNRESET
    10060,  # WSAETIMEDOUT
    10061,  # WSAECONNREFUSED
    10065,  # WSAEHOSTUNREACH
}


class BridgeHandoffOutcomeUnknown(RuntimeError):
    """The target may have committed a handoff whose signed reply was lost."""

    def __init__(self, transfer_id: str, cause: BaseException):
        self.transfer_id = str(transfer_id or "unknown")
        self.cause = cause
        super().__init__(
            f"handoff outcome is unknown for {self.transfer_id}: {cause}"
        )


class TransferRedirectRequired(RuntimeError):
    """A frontend request reached a Session whose transfer is already accepted."""

    def __init__(self, redirect: Mapping[str, Any]):
        self.redirect = dict(redirect)
        super().__init__(
            _TRANSFER_REDIRECT_ERROR_PREFIX
            + json.dumps(self.redirect, ensure_ascii=True, sort_keys=True)
        )


def transfer_redirect_snapshot(runtime: Any) -> dict[str, str] | None:
    """Return the minimal accepted-transfer projection exposed to FC owners."""

    state = getattr(runtime, "_transfer_state", None)
    if not isinstance(state, Mapping):
        if hasattr(runtime, "_transfer_state"):
            return None
        metadata = getattr(runtime, "metadata", None)
        if not isinstance(metadata, Mapping) and getattr(
            runtime, "is_function_worker_proxy", False
        ):
            getter = getattr(runtime, "get_runtime_metadata", None)
            metadata = getter() if callable(getter) else None
        state = (
            metadata.get("transfer_redirect")
            if isinstance(metadata, Mapping)
            else None
        )
    if not isinstance(state, Mapping):
        return None
    status = str(state.get("status") or "")
    outcome_unknown = status == "unknown" or (
        status == "pending" and state.get("outcome_unknown") is True
    )
    if status != "accepted" and not outcome_unknown:
        return None
    snapshot = {
        "status": "unknown" if outcome_unknown else "accepted",
        "transfer_id": str(state.get("transfer_id") or "unknown").strip(),
        "target_agent": str(state.get("target_agent") or "target").strip(),
        "target_instance": str(state.get("target_instance") or "unknown").strip(),
    }
    if any(not value for value in snapshot.values()):
        return None
    return snapshot


def _transfer_fence_path(runtime: Any) -> Path | None:
    raw_path = getattr(runtime, "transfer_state_path", None)
    if raw_path is None:
        workspace_dir = getattr(runtime, "workspace_dir", None)
        if workspace_dir is None:
            return None
        raw_path = Path(str(workspace_dir)) / "active_transfer.json"
    path = Path(str(raw_path))
    return path if path.is_absolute() else None


def transfer_admission_lock(runtime: Any) -> Any:
    """Return the per-runtime lock that linearizes fence flips and Run writes."""

    lock = getattr(runtime, "_transfer_admission_lock", None)
    if lock is None:
        lock = threading.RLock()
        runtime._transfer_admission_lock = lock
    return lock


def _unavailable_transfer_fence_snapshot(runtime: Any) -> dict[str, str]:
    return {
        "status": "unknown",
        "transfer_id": "unknown",
        "target_agent": str(getattr(runtime, "name", None) or "target"),
        "target_instance": "unknown",
    }


def authoritative_transfer_redirect_snapshot(runtime: Any) -> dict[str, str] | None:
    """Read the exact runtime fence when a worker metadata snapshot is stale.

    A missing fence means no durable transfer fact.  Once the trusted runtime
    workspace contains a fence, an unreadable or malformed value is kept
    fail-closed instead of being confused with an absent transfer.
    """

    snapshot = transfer_redirect_snapshot(runtime)
    if snapshot is not None:
        return snapshot
    path = _transfer_fence_path(runtime)
    if path is None:
        return None
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_TRANSFER_FENCE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        return _unavailable_transfer_fence_snapshot(runtime)
    if len(raw) > _MAX_TRANSFER_FENCE_BYTES:
        return _unavailable_transfer_fence_snapshot(runtime)
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return _unavailable_transfer_fence_snapshot(runtime)
    if not isinstance(state, Mapping):
        return _unavailable_transfer_fence_snapshot(runtime)
    status = state.get("status")
    outcome_unknown = state.get("outcome_unknown", False)
    if (
        status not in {"pending", "accepted", "unknown"}
        or not isinstance(outcome_unknown, bool)
        or any(
            not isinstance(state.get(key), str) or not state.get(key).strip()
            for key in ("transfer_id", "target_agent", "target_instance")
        )
    ):
        return _unavailable_transfer_fence_snapshot(runtime)
    holder = type("_TransferFenceProjection", (), {"_transfer_state": state})()
    return transfer_redirect_snapshot(holder)


def session_request_requires_transfer_fence(
    request_content: Mapping[str, Any] | None,
) -> bool:
    """Keep the non-voice fence when any ordinary attachment is present."""

    from orchestrator.multimodal_contract import attachment_manifest

    manifest = attachment_manifest(request_content)
    voice_attachments = [
        item
        for item in manifest
        if item.get("modality") == "audio"
        and item.get("semantic_role") == "voice_message"
    ]
    return not voice_attachments or len(voice_attachments) != len(manifest)


def transfer_redirect_from_exception(exc: BaseException) -> dict[str, str] | None:
    """Recover the typed redirect across the Function Worker error envelope."""

    if isinstance(exc, TransferRedirectRequired):
        raw = exc.redirect
    else:
        remote = getattr(exc, "error", None)
        if not isinstance(remote, Mapping) or str(remote.get("type")) != (
            "TransferRedirectRequired"
        ):
            return None
        message = str(remote.get("message") or "")
        if not message.startswith(_TRANSFER_REDIRECT_ERROR_PREFIX):
            return None
        try:
            raw = json.loads(message[len(_TRANSFER_REDIRECT_ERROR_PREFIX) :])
        except (json.JSONDecodeError, TypeError):
            return None
    holder = type("_TransferProjection", (), {"_transfer_state": raw})()
    return transfer_redirect_snapshot(holder)


def require_untransferred(runtime: Any) -> None:
    """Fail before a post-transfer ingress can create a Run or transport effect."""

    redirect = transfer_redirect_snapshot(runtime)
    if redirect is not None:
        raise TransferRedirectRequired(redirect)


def transfer_redirect_text_from_snapshot(state: Mapping[str, Any]) -> str:
    target_agent = state.get("target_agent") or "target"
    target_instance = state.get("target_instance") or "unknown"
    transfer_id = state.get("transfer_id") or "unknown"
    if str(state.get("status") or "") == "unknown":
        return ui_language.tr("transfer.unknown", transfer_id=transfer_id)
    return (
        f"This session has been transferred to {target_agent}@{target_instance}.\n"
        f"Continue there. Transfer ID: {transfer_id}"
    )


def persist_transfer_state(runtime: Any) -> None:
    if runtime._transfer_state is None:
        runtime.transfer_state_path.unlink(missing_ok=True)
        return
    runtime.transfer_state_path.write_text(
        json.dumps(runtime._transfer_state, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def clear_transfer_state(runtime: Any) -> None:
    with transfer_admission_lock(runtime):
        runtime._transfer_state = None
        runtime._suppressed_transfer_results.clear()
        runtime._persist_transfer_state()


def record_transfer_outcome_unknown(
    runtime: Any,
    *,
    transfer_id: str,
    error: BaseException,
) -> None:
    """Preserve the pending fence and identity without claiming success or failure."""

    with transfer_admission_lock(runtime):
        state = getattr(runtime, "_transfer_state", None)
        if not isinstance(state, dict) or str(state.get("transfer_id")) != str(
            transfer_id
        ):
            raise ValueError("active transfer identity changed before unknown outcome")
        state["status"] = "pending"
        state["outcome_unknown"] = True
        state["unknown_at"] = datetime.now().isoformat()
        state["error"] = str(error)
        runtime._persist_transfer_state()


def record_transfer_accepted(
    runtime: Any,
    *,
    transfer_id: str,
    target_status: str,
) -> None:
    """Commit an accepted fence against the same boundary as Run admission."""

    with transfer_admission_lock(runtime):
        state = getattr(runtime, "_transfer_state", None)
        if not isinstance(state, dict) or str(state.get("transfer_id")) != str(
            transfer_id
        ):
            raise ValueError("active transfer identity changed before acceptance")
        state["status"] = "accepted"
        state["target_status"] = str(target_status)
        runtime._persist_transfer_state()


def has_active_transfer(runtime: Any) -> bool:
    return bool(runtime._transfer_state and runtime._transfer_state.get("status") in {"pending", "accepted"})


def transfer_redirect_text(runtime: Any) -> str:
    state = transfer_redirect_snapshot(runtime) or getattr(
        runtime, "_transfer_state", None
    ) or {}
    return transfer_redirect_text_from_snapshot(state)


def should_redirect_after_transfer(runtime: Any) -> bool:
    return transfer_redirect_snapshot(runtime) is not None


def should_buffer_during_transfer(runtime: Any, request_id: str | None) -> bool:
    if not runtime._transfer_state:
        return False
    status = runtime._transfer_state.get("status")
    if status not in {"pending", "accepted"}:
        return False
    cutoff_seq = runtime._transfer_state.get("cutoff_seq")
    req_seq = runtime._parse_request_seq(request_id)
    return cutoff_seq is not None and req_seq is not None and req_seq <= cutoff_seq


def record_suppressed_transfer_result(
    runtime: Any,
    item: QueuedRequest,
    *,
    success: bool,
    text: str | None = None,
    error: str | None = None,
) -> None:
    runtime._suppressed_transfer_results.append(
        {
            "request_id": item.request_id,
            "chat_id": item.chat_id,
            "success": success,
            "text": text,
            "error": error,
            "summary": item.summary,
            "source": item.source,
        }
    )


async def flush_suppressed_transfer_results(runtime: Any) -> None:
    buffered = list(runtime._suppressed_transfer_results)
    runtime._suppressed_transfer_results.clear()
    for entry in buffered:
        text = (
            entry.get("text")
            if entry.get("success")
            else (
                f"Flex Backend Error ({runtime.config.active_backend}): "
                f"{format_backend_error_for_user(runtime.config.active_backend, entry.get('error') or '', locale=ui_language.preferred_locale(runtime, actor_id=entry.get('chat_id')))}"
            )
        )
        if not text:
            continue
        await runtime.send_long_message(
            chat_id=entry["chat_id"],
            text=text,
            request_id=entry.get("request_id"),
            purpose="transfer-release",
        )


def strip_transfer_accept_prefix(item: QueuedRequest, text: str) -> str:
    if not item.source.startswith("bridge-transfer:"):
        return text
    prefix = f"TRANSFER_ACCEPTED {item.source.split(':', 1)[1]}"
    if not text.startswith(prefix):
        return text
    stripped = text[len(prefix):].lstrip()
    if stripped.startswith("\n"):
        stripped = stripped.lstrip()
    return stripped


def _render_http_endpoint(host: str, port: int, path: str) -> str:
    normalized_host = str(host or "").strip().strip("[]")
    if not normalized_host or normalized_host in {"0.0.0.0", "::"}:
        raise ValueError("instance has no connectable Workbench host")
    rendered = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    return f"http://{rendered}:{int(port)}{path}"


def _local_workbench_endpoint(runtime: Any, current_instance: str) -> tuple[str, int]:
    orchestrator = getattr(runtime, "orchestrator", None)
    resolver = getattr(orchestrator, "resolve_service_endpoint", None)
    if callable(resolver):
        endpoint = resolver("workbench", expected_instance=current_instance)
        return str(endpoint["host"]), int(endpoint["port"])
    registry = getattr(orchestrator, "endpoint_registry", None)
    if registry is not None:
        endpoint = registry.resolve(
            "workbench",
            expected_instance=current_instance,
        )
        return endpoint.host, endpoint.port
    raise ServiceEndpointError(
        "live Workbench endpoint is unavailable from the runtime capability context"
    )


def resolve_bridge_handoff_endpoint(
    runtime: Any,
    target_instance: str,
    mode: str,
) -> tuple[str, str]:
    action = "fork" if str(mode or "").strip().lower() == "fork" else "transfer"
    normalized_target = runtime._normalize_instance_name(target_instance)
    current_instance = runtime._normalize_instance_name(runtime._detect_instance_name())
    local_host, local_port = _local_workbench_endpoint(runtime, current_instance)
    if normalized_target == current_instance:
        return current_instance, _render_http_endpoint(
            local_host,
            local_port,
            f"/api/bridge/{action}",
        )

    instances = runtime._load_instances()
    for name, inst in instances.items():
        if runtime._normalize_instance_name(name) != normalized_target:
            continue
        declared_instance = runtime._normalize_instance_name(
            inst.get("instance_id") or name
        )
        if declared_instance != normalized_target:
            raise ValueError(
                "cross-instance Workbench route rejected: "
                f"target={normalized_target} discovered={declared_instance}"
            )
        candidates = candidate_remote_base_urls(inst)
        if not candidates:
            raise ValueError(f"instance {normalized_target} has no Remote route configured")
        base_url = candidates[0].rstrip("/")
        split = urlsplit(base_url)
        if (
            str(split.hostname or "").casefold() == local_host.casefold()
            and int(split.port or (443 if split.scheme == "https" else 80)) == local_port
        ):
            raise ValueError(
                "cross-instance Remote route rejected: target resolves to the local "
                "Workbench endpoint"
            )
        return (
            normalized_target,
            f"{base_url}/workbench/v1/proxy/api/bridge/{action}",
        )
    raise ValueError(f"unknown instance: {target_instance}")


def handoff_health_endpoint(handoff_endpoint: str) -> str:
    remote_marker = "/workbench/v1/proxy/api/bridge/"
    if remote_marker in str(handoff_endpoint):
        return str(handoff_endpoint).split(remote_marker, 1)[0] + "/workbench/v1/status"
    marker = "/api/bridge/"
    if marker not in str(handoff_endpoint):
        raise ValueError("invalid Workbench handoff endpoint")
    return str(handoff_endpoint).split(marker, 1)[0] + "/api/health"


def _verify_remote_handoff_status(
    payload: Mapping[str, Any],
    *,
    expected_source: str,
    expected_target: str,
) -> None:
    instance = payload.get("instance") if isinstance(payload, Mapping) else None
    health = payload.get("workbench_health") if isinstance(payload, Mapping) else None
    source = normalize_instance_id(payload.get("authenticated_instance"))
    target = normalize_instance_id(
        instance.get("instance_id") if isinstance(instance, Mapping) else None
    )
    health_target = normalize_instance_id(
        health.get("instance_id") if isinstance(health, Mapping) else None
    )
    if (
        payload.get("gateway") != "workbench_v1"
        or payload.get("workbench_online") is not True
        or source != normalize_instance_id(expected_source)
        or target != normalize_instance_id(expected_target)
        or health_target != normalize_instance_id(expected_target)
    ):
        raise ValueError("cross-instance Workbench identity check failed")


def _remote_handoff_candidate_endpoints(
    runtime: Any,
    *,
    endpoint: str,
    expected_target: str,
) -> list[str]:
    marker = "/workbench/v1/proxy/api/bridge/"
    path = urlsplit(endpoint).path
    if path not in {marker + "transfer", marker + "fork"}:
        raise ValueError("invalid cross-instance Workbench handoff endpoint")

    target = runtime._normalize_instance_name(expected_target)
    for name, raw in runtime._load_instances().items():
        if not isinstance(raw, Mapping):
            continue
        declared = runtime._normalize_instance_name(raw.get("instance_id") or name)
        if runtime._normalize_instance_name(name) != target and declared != target:
            continue
        if declared != target:
            raise ValueError(
                "cross-instance Workbench route rejected: "
                f"target={target} discovered={declared}"
            )
        bases = candidate_remote_base_urls(raw)[:_MAX_REMOTE_HANDOFF_CANDIDATES]
        if not bases:
            raise ValueError(f"instance {target} has no Remote route configured")
        return [base.rstrip("/") + path for base in bases]
    raise ValueError(f"unknown instance: {expected_target}")


def _is_remote_candidate_connect_failure(exc: BaseException) -> bool:
    """Return true only when no authenticated HTTP response could exist."""

    current: BaseException | object | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, HTTPError):
            return False
        if isinstance(current, URLError):
            current = current.reason
            continue
        if isinstance(current, (TimeoutError, socket.timeout, socket.gaierror)):
            return True
        if isinstance(current, OSError):
            return current.errno in _RETRYABLE_CONNECT_ERRNOS
        current = getattr(current, "__cause__", None)
    return False


def _request_remote_handoff(
    runtime: Any,
    *,
    endpoint: str,
    expected_target: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    token = load_shared_token(runtime_remote.instance_root(runtime))
    source = normalize_instance_id(runtime._detect_instance_name())
    if not token or not source:
        raise ValueError("authenticated HASHI Remote Session transfer is unavailable")
    selected_endpoint = None
    last_connect_error: AgentMoveRemoteError | None = None
    for candidate_endpoint in _remote_handoff_candidate_endpoints(
        runtime,
        endpoint=endpoint,
        expected_target=expected_target,
    ):
        try:
            status = request_authenticated_json(
                handoff_health_endpoint(candidate_endpoint),
                method="GET",
                shared_token=token,
                from_instance=source,
                timeout=10,
            )
        except AgentMoveRemoteError as exc:
            if not _is_remote_candidate_connect_failure(exc):
                raise
            last_connect_error = exc
            continue
        _verify_remote_handoff_status(
            status,
            expected_source=source,
            expected_target=expected_target,
        )
        selected_endpoint = candidate_endpoint
        break
    if selected_endpoint is None:
        if last_connect_error is not None:
            raise last_connect_error
        raise AgentMoveRemoteError(
            f"no reachable authenticated Remote route for {expected_target}"
        )
    try:
        return request_authenticated_json(
            selected_endpoint,
            method="POST",
            payload=dict(payload),
            shared_token=token,
            from_instance=source,
            timeout=_BRIDGE_HANDOFF_CLIENT_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise BridgeHandoffOutcomeUnknown(
            str(payload.get("transfer_id") or "unknown"),
            exc,
        ) from exc


async def send_bridge_handoff(
    runtime: Any,
    *,
    endpoint: str,
    expected_target: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Send once; a transport failure after POST is an unknown outcome, never replayed."""

    if "/workbench/v1/proxy/api/bridge/" in urlsplit(endpoint).path:
        return await asyncio.to_thread(
            _request_remote_handoff,
            runtime,
            endpoint=endpoint,
            expected_target=expected_target,
            payload=payload,
        )

    timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        health_endpoint = handoff_health_endpoint(endpoint)
        async with session.get(health_endpoint) as health_response:
            health = await health_response.json()
            if health_response.status >= 400:
                raise RuntimeError(
                    str(health.get("error") or f"HTTP {health_response.status}")
                )
            verify_handoff_instance_identity(
                health,
                expected_instance=expected_target,
            )
        async with session.post(endpoint, json=dict(payload)) as response:
            body = await response.json()
            if response.status >= 400 or not body.get("ok"):
                raise RuntimeError(str(body.get("error") or f"HTTP {response.status}"))
            return body


def verify_handoff_instance_identity(
    health_payload: Any,
    *,
    expected_instance: str,
) -> None:
    if not isinstance(health_payload, dict):
        raise ValueError("Workbench health response is not an object")
    received = str(health_payload.get("instance_id") or "").strip().upper()
    expected = str(expected_instance or "").strip().upper()
    if not received or received != expected:
        raise ValueError(
            "cross-instance Workbench identity check failed: "
            f"expected={expected or '<missing>'} received={received or '<missing>'}"
        )


def build_handoff_payload(
    runtime: Any,
    target_agent: str,
    target_instance: str,
    mode: str,
    *,
    handoff_builder: Any | None = None,
) -> dict[str, Any]:
    action = "fork" if str(mode or "").strip().lower() == "fork" else "transfer"
    transfer_id = f"{'frk' if action == 'fork' else 'trf'}-{uuid4().hex}"
    source_instance = runtime._normalize_instance_name(runtime._detect_instance_name())
    builder = handoff_builder or runtime.handoff_builder
    package = builder.build_transfer_package(
        transfer_id=transfer_id,
        source_agent=runtime.name,
        source_instance=source_instance,
        target_agent=target_agent,
        target_instance=target_instance,
        created_at=datetime.now().isoformat(),
        max_rounds=10,
        max_words=6000,
    )
    package["mode"] = action
    package["source_runtime"] = runtime.get_runtime_metadata()
    package["source_workspace_dir"] = str(runtime.workspace_dir)
    package["source_transcript_path"] = str(runtime.transcript_log_path)
    return package
