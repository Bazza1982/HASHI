from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from orchestrator.runtime_common import QueuedRequest


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
            else f"Flex Backend Error ({runtime.config.active_backend}): {entry.get('error')}"
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


def resolve_bridge_handoff_endpoint(runtime: Any, target_instance: str, mode: str) -> tuple[str, str]:
    action = "fork" if str(mode or "").strip().lower() == "fork" else "transfer"
    normalized_target = runtime._normalize_instance_name(target_instance)
    current_instance = runtime._normalize_instance_name(runtime._detect_instance_name())
    if normalized_target == current_instance:
        return current_instance, f"http://127.0.0.1:{runtime.global_config.workbench_port}/api/bridge/{action}"

    instances = runtime._load_instances()
    for name, inst in instances.items():
        if runtime._normalize_instance_name(name) != normalized_target:
            continue
        host = str(inst.get("api_host") or "127.0.0.1").strip() or "127.0.0.1"
        port = inst.get("workbench_port")
        if not port:
            raise ValueError(f"instance {normalized_target} has no workbench_port configured")
        return normalized_target, f"http://{host}:{int(port)}/api/bridge/{action}"
    raise ValueError(f"unknown instance: {target_instance}")


def build_handoff_payload(runtime: Any, target_agent: str, target_instance: str, mode: str) -> dict[str, Any]:
    action = "fork" if str(mode or "").strip().lower() == "fork" else "transfer"
    transfer_id = f"{'frk' if action == 'fork' else 'trf'}-{uuid4().hex}"
    source_instance = runtime._normalize_instance_name(runtime._detect_instance_name())
    package = runtime.handoff_builder.build_transfer_package(
        transfer_id=transfer_id,
        source_agent=runtime.name,
        source_instance=source_instance,
        target_agent=target_agent,
        target_instance=target_instance,
        created_at=datetime.now().isoformat(),
        max_rounds=30,
        max_words=18000,
    )
    package["mode"] = action
    package["source_runtime"] = runtime.get_runtime_metadata()
    package["source_workspace_dir"] = str(runtime.workspace_dir)
    package["source_transcript_path"] = str(runtime.transcript_log_path)
    return package
