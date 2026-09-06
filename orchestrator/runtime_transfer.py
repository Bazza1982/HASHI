from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from orchestrator.runtime_common import QueuedRequest
from orchestrator.runtime_delivery import format_backend_error_for_user
from orchestrator import ui_language
from orchestrator.service_endpoints import ServiceEndpointError


def persist_transfer_state(runtime: Any) -> None:
    if runtime._transfer_state is None:
        runtime.transfer_state_path.unlink(missing_ok=True)
        return
    runtime.transfer_state_path.write_text(
        json.dumps(runtime._transfer_state, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def clear_transfer_state(runtime: Any) -> None:
    runtime._transfer_state = None
    runtime._suppressed_transfer_results.clear()
    runtime._persist_transfer_state()


def has_active_transfer(runtime: Any) -> bool:
    return bool(runtime._transfer_state and runtime._transfer_state.get("status") in {"pending", "accepted"})


def transfer_redirect_text(runtime: Any) -> str:
    state = runtime._transfer_state or {}
    target_agent = state.get("target_agent") or "target"
    target_instance = state.get("target_instance") or "unknown"
    transfer_id = state.get("transfer_id") or "unknown"
    return (
        f"This session has been transferred to {target_agent}@{target_instance}.\n"
        f"Continue there. Transfer ID: {transfer_id}"
    )


def should_redirect_after_transfer(runtime: Any) -> bool:
    return bool(runtime._transfer_state and runtime._transfer_state.get("status") == "accepted")


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
        host = str(
            inst.get("workbench_host")
            or inst.get("api_host")
            or inst.get("host")
            or ""
        ).strip()
        port = inst.get("workbench_port")
        if not port:
            raise ValueError(f"instance {normalized_target} has no workbench_port configured")
        if host.casefold() == local_host.casefold() and int(port) == local_port:
            raise ValueError(
                "cross-instance Workbench route rejected: target resolves to the "
                "local instance endpoint"
            )
        return normalized_target, _render_http_endpoint(
            host,
            int(port),
            f"/api/bridge/{action}",
        )
    raise ValueError(f"unknown instance: {target_instance}")


def handoff_health_endpoint(handoff_endpoint: str) -> str:
    marker = "/api/bridge/"
    if marker not in str(handoff_endpoint):
        raise ValueError("invalid Workbench handoff endpoint")
    return str(handoff_endpoint).split(marker, 1)[0] + "/api/health"


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
