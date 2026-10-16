"""Ollama-native capacity checks and bounded projections of new tool results.

This is HERV3 Function policy, scoped to the existing native-context opt-in.
The service's tokenizer, not the shared character estimate, admits generation.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from copy import deepcopy
from pathlib import Path

from adapters.ollama_native import call_native_chat
from adapters.openrouter_api import (
    ProviderContextCapacityPreflightError,
    ProviderProtocolForensicError,
    _effective_protocol_parameters,
    _utc_timestamp,
)


_CLOSING_NOTICE = (
    "HERV3 capacity status: tool use is closed for this turn. This is not a new "
    "user task. Answer the original request using only visible evidence and "
    "state remaining work. Saved but unshown content has not been read. "
    "Do not request or replay tools."
)


def closing_messages(messages):
    rows = list(messages)
    notice = {"role": "user", "content": _CLOSING_NOTICE}
    if not rows or rows[-1] != notice:
        rows.append(notice)
    return rows


def close_tool_loop(adapter, payload):
    adapter._ollama_context_closing = True
    payload.pop("tools", None)
    payload["messages"] = closing_messages(payload["messages"])


async def check_native_capacity(
    adapter, payload, *, request_id, provider_calls, allow_close=True,
):
    # Ollama 0.35 has no public tokenizer/count API. num_predict=0 selects its
    # default generation budget; it is NOT a count-only request. Use exactly
    # one token with the same messages/tools/template/context and no callbacks.
    probe = deepcopy(payload)
    probe["options"]["num_predict"] = 1
    probe["stream"] = False
    adapter._ollama_capacity_probe_serial += 1
    serial = adapter._ollama_capacity_probe_serial
    probe_id = f"{request_id}:capacity:{serial}"
    started = time.perf_counter()
    started_at = _utc_timestamp()
    refs = [adapter._record_provider_wire_evidence(
        "capacity_probe_prepared", request_id=request_id, call_serial=serial,
        payload={"purpose": "context_capacity_probe", "body": probe},
    )]
    try:
        result = await call_native_chat(adapter, probe, adapter._request_headers(), None)
    except BaseException as exc:
        response = getattr(exc, "response", None)
        refs.append(adapter._record_provider_wire_evidence(
            "capacity_probe_failed", request_id=request_id, call_serial=serial,
            payload={
                "purpose": "context_capacity_probe",
                "error_type": type(exc).__name__,
                "http_status": getattr(response, "status_code", None),
                "response_body": response.text if response is not None else None,
                "partial_protocol": dict(getattr(exc, "hashi_provider_protocol", {}) or {}),
            },
        ))
        provider_calls.append(adapter._provider_call_record(
            request_id=probe_id, serial=serial, payload={
                "hashi_request_id": request_id, "purpose": "context_capacity_probe",
                "input": 0, "output": 0, "thinking": 0, "token_source": "unknown",
                "thinking_in_output": True, "cost_usd": 0.0,
                "status": "failed_without_receipt", "decision_success": False,
                "decision": "capacity_probe_failed", "decision_reason": type(exc).__name__,
                "provider_call_latency_ms": (time.perf_counter() - started) * 1000,
                "request_started_at": started_at, "response_observed_at": _utc_timestamp(),
                "effective_parameters": _effective_protocol_parameters(probe),
                "provider_wire_evidence_refs": refs,
            },
        ))
        raise

    valid_count = result.provider_compatibility.get("native_prompt_count_valid") is True
    bounded_probe = (
        result.provider_compatibility.get("native_output_count_valid") is True
        and 0 <= result.completion_tokens <= 1
        and result.finish_reason in {"length", "stop", "tool_calls"}
    )
    capacity = adapter._native_capacity()
    fits = valid_count and bounded_probe and result.prompt_tokens <= capacity.usable_input_tokens
    refs.append(adapter._record_provider_wire_evidence(
        "capacity_probe_received", request_id=request_id, call_serial=serial,
        payload={"purpose": "context_capacity_probe", "wire": result.wire_evidence},
    ))
    provider_calls.append(adapter._provider_call_record(
        request_id=probe_id, serial=serial, payload={
            "hashi_request_id": request_id, "purpose": "context_capacity_probe",
            "input": result.prompt_tokens, "output": result.completion_tokens,
            "thinking": result.thinking_tokens, "token_source": "provider" if valid_count else "unknown",
            "thinking_in_output": True, "cost_usd": 0.0, "status": "completed",
            "decision": "capacity_accepted" if fits else "capacity_rejected",
            "decision_success": fits, "usable_input_tokens": capacity.usable_input_tokens,
            "provider_call_latency_ms": (time.perf_counter() - started) * 1000,
            "request_started_at": started_at, "response_observed_at": _utc_timestamp(),
            "effective_parameters": _effective_protocol_parameters(probe),
            "provider_wire_evidence_refs": refs,
        },
    ))
    if not valid_count or not bounded_probe:
        raise ProviderContextCapacityPreflightError(
            "Ollama did not provide a valid bounded capacity probe; full generation "
            "was not started and no tools were executed by the probe."
        )
    if (
        allow_close and payload.get("tools")
        and result.prompt_tokens > capacity.usable_input_tokens - 2048
    ):
        # Reclaim future tool schemas, not the task or completed evidence.
        # The changed final payload must pass its own native probe. Never
        # recover by replaying tools or shrinking the response reserve.
        close_tool_loop(adapter, payload)
        await check_native_capacity(
            adapter, payload, request_id=request_id,
            provider_calls=provider_calls, allow_close=False,
        )
        return
    if not fits:
        raise ProviderContextCapacityPreflightError(
            f"Ollama counted {result.prompt_tokens} input tokens; the usable input "
            f"capacity is {capacity.usable_input_tokens}, preserving "
            f"{capacity.response_headroom_tokens} reasoning/output tokens. "
            "The task and completed tool results have not been discarded."
        )
    adapter._ollama_prompt_tokens = result.prompt_tokens


def _save_result(adapter, output, *, directory="ollama_tool_results"):
    from tools.private_files import protect_private_file

    raw = output.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    root = Path(adapter.config.workspace_dir) / "backend_state" / directory
    try:
        if not root.exists():
            root.mkdir(parents=True, exist_ok=True)
            protect_private_file(root)
        path = root / (digest + ".txt")
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise OSError("Saved tool result hash mismatch")
        else:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
    except OSError as exc:
        raise ProviderProtocolForensicError(
            "The full tool result could not be preserved; no paged projection was substituted."
        ) from exc
    return path, digest


def page_new_tool_results(adapter, messages, first_result):
    """Page only this batch's new outputs; never prune PCM/task/earlier results."""
    prompt_tokens = getattr(adapter, "_ollama_prompt_tokens", None)
    if prompt_tokens is None:
        return
    results = [row for row in messages[first_result:] if row.get("role") == "tool"]
    if not results:
        return
    remaining = max(
        0, adapter._native_capacity().usable_input_tokens - prompt_tokens
        - getattr(adapter, "_ollama_last_output_tokens", 0) - 512 - 256 * len(results)
    )
    # UTF-8 bytes are a conservative page-size allowance, not a claimed token
    # count. The exact, complete next payload still passes the native probe.
    allowance = remaining // len(results)
    if allowance < 1024:
        # Do not invite repeated reads of footer-only pages. The next call
        # closes tools and is independently counted without future schemas.
        adapter._ollama_context_closing = True
    for row in results:
        original = row.get("content", "")
        if not isinstance(original, str) or len(original.encode("utf-8")) <= allowance:
            continue
        path, digest = _save_result(adapter, original)
        metadata = {
            "saved_result": str(path), "sha256": digest,
            "total_chars": len(original), "shown_chars": 0, "next_line": 1,
        }
        instruction = (
            "\nRead saved_result with file_read offset/limit or log_query. "
            "Unshown text is unread; never rerun the original tool."
        )
        def render(prefix):
            metadata["shown_chars"] = len(prefix)
            metadata["next_line"] = prefix.count("\n") + 1
            return prefix + "\nHASHI_OLLAMA_RESULT_PAGE " + json.dumps(
                metadata, ensure_ascii=False, separators=(",", ":")
            ) + instruction

        footer_bytes = len(render("").encode("utf-8")) + 32
        prefix = original.encode("utf-8")[:max(0, allowance - footer_bytes)].decode("utf-8", errors="ignore")
        # End on a complete line so the saved artifact's next_line is usable.
        newline = prefix.rfind("\n")
        prefix = prefix[:newline + 1] if newline >= 0 else ""
        projected = render(prefix)
        context = getattr(adapter, "_active_provider_wire_context", {}) or {}
        adapter._record_provider_wire_evidence(
            "tool_result_paged",
            request_id=str(context.get("request_id") or ""),
            call_serial=int(context.get("call_serial") or 1),
            payload={
                "tool_call_id": row.get("tool_call_id"), "full_output": original,
                "page": dict(metadata), "remaining_input_allowance": remaining,
            },
        )
        row["content"] = projected
