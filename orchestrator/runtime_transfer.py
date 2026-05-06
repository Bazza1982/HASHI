from __future__ import annotations

import json
from typing import Any

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
