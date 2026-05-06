from __future__ import annotations

import asyncio
import json
import re
from contextlib import suppress
from datetime import datetime
from typing import Any, Mapping

from orchestrator.audit_mode import (
    AuditProcessor,
    AuditTelemetryCollector,
    format_audit_report,
    load_audit_config,
    should_audit_source,
    should_notify_audit_result,
)
from orchestrator.runtime_common import QueuedRequest


def audit_enabled(runtime: Any) -> bool:
    return getattr(runtime.backend_manager, "agent_mode", "flex") == "audit"


def audit_timeout_s(runtime: Any) -> float:
    state = runtime.backend_manager.get_state_snapshot()
    cfg = load_audit_config(state)
    return cfg.timeout_s


def audit_visible_context(runtime: Any, context_window: int) -> list[dict[str, str]]:
    return runtime._wrapper_visible_context(context_window)


def build_audit_telemetry(
    runtime: Any,
    item: QueuedRequest,
    response: Any,
    collector: AuditTelemetryCollector | None,
) -> dict[str, Any]:
    stream = collector.to_dict() if collector is not None else {}
    tool_call_count = int(getattr(response, "tool_call_count", 0) or 0)
    if tool_call_count <= 0:
        tool_call_count = int(stream.get("action_event_count", 0) or 0)
    return {
        **stream,
        "request_id": item.request_id,
        "source": item.source,
        "summary": item.summary,
        "silent": item.silent,
        "backend": runtime.config.active_backend,
        "model": runtime.get_current_model(),
        "tool_call_count": tool_call_count,
        "tool_loop_count": int(getattr(response, "tool_loop_count", 0) or 0),
        "usage": {
            "input_tokens": int(getattr(getattr(response, "usage", None), "input_tokens", 0) or 0),
            "output_tokens": int(getattr(getattr(response, "usage", None), "output_tokens", 0) or 0),
            "thinking_tokens": int(getattr(getattr(response, "usage", None), "thinking_tokens", 0) or 0),
        },
        "risk_flags": {
            "codex_cli_backend": runtime.config.active_backend == "codex-cli",
            "thinking_trace_is_observable_only": True,
        },
    }


def append_audit_transcript(
    runtime: Any,
    item: QueuedRequest,
    *,
    core_raw: str,
    visible_text: str,
    telemetry: Mapping[str, Any],
    audit_result: Any,
    completion_path: str,
) -> None:
    path = getattr(runtime, "audit_transcript_log_path", None) or (runtime.workspace_dir / "audit_transcript.jsonl")
    entry = {
        "role": "audit",
        "request_id": item.request_id,
        "source": item.source,
        "summary": item.summary,
        "completion_path": completion_path,
        "backend": runtime.config.active_backend,
        "model": runtime.get_current_model(),
        "core_raw": core_raw or "",
        "visible_text": visible_text or "",
        "telemetry": telemetry,
        "audit_result": {
            "status": getattr(audit_result, "status", "pass"),
            "max_severity": getattr(audit_result, "max_severity", "none"),
            "findings": [finding.__dict__ for finding in getattr(audit_result, "findings", ())],
            "triggered_sensors": list(getattr(audit_result, "triggered_sensors", ())),
            "summary": getattr(audit_result, "summary", ""),
            "reasoning": getattr(audit_result, "reasoning", ""),
            "audit_used": bool(getattr(audit_result, "audit_used", False)),
            "audit_failed": bool(getattr(audit_result, "audit_failed", False)),
            "fallback_reason": getattr(audit_result, "fallback_reason", None),
            "latency_ms": round(float(getattr(audit_result, "latency_ms", 0.0) or 0.0), 3),
            "raw_text": getattr(audit_result, "raw_text", ""),
        },
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        runtime.error_logger.warning(f"Failed to append audit transcript: {exc}")


def write_audit_evidence(
    runtime: Any,
    item: QueuedRequest,
    *,
    core_raw: str,
    visible_text: str,
    telemetry: Mapping[str, Any],
    completion_path: str,
    audit_criteria: Mapping[str, Any] | None,
    visible_context: list[dict[str, str]],
) -> str:
    safe_request_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", item.request_id or "request")
    path = runtime.workspace_dir / "audit_evidence" / f"{safe_request_id}.json"
    entry = {
        "role": "audit_evidence",
        "request_id": item.request_id,
        "source": item.source,
        "summary": item.summary,
        "user_request": item.prompt,
        "completion_path": completion_path,
        "backend": runtime.config.active_backend,
        "model": runtime.get_current_model(),
        "core_raw": core_raw or "",
        "visible_text": visible_text or "",
        "telemetry": telemetry,
        "audit_criteria": dict(audit_criteria or {}),
        "recent_visible_context": visible_context,
        "related_logs": {
            "core_transcript": str(runtime.workspace_dir / "core_transcript.jsonl"),
            "audit_transcript": str(runtime.workspace_dir / "audit_transcript.jsonl"),
            "token_audit": str(runtime.workspace_dir / "token_audit.jsonl"),
        },
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)
    except Exception as exc:
        runtime.error_logger.warning(f"Failed to write audit evidence for {item.request_id}: {exc}")
        return ""


def schedule_audit_followup(
    runtime: Any,
    item: QueuedRequest,
    *,
    core_raw: str,
    visible_text: str,
    response: Any,
    audit_collector: AuditTelemetryCollector | None,
    completion_path: str,
) -> None:
    if not runtime._audit_enabled() or not should_audit_source(item.source):
        return
    task = asyncio.create_task(
        runtime._run_audit_followup(
            item,
            core_raw=core_raw,
            visible_text=visible_text,
            response=response,
            audit_collector=audit_collector,
            completion_path=completion_path,
        )
    )
    background_tasks = getattr(runtime, "_background_tasks", None)
    if background_tasks is not None:
        background_tasks.add(task)

    def _on_done(done: asyncio.Task) -> None:
        if background_tasks is not None:
            background_tasks.discard(done)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            with suppress(Exception):
                runtime.error_logger.warning(f"Audit followup failed for {item.request_id}: {exc}")

    task.add_done_callback(_on_done)


async def run_audit_followup(
    runtime: Any,
    item: QueuedRequest,
    *,
    core_raw: str,
    visible_text: str,
    response: Any,
    audit_collector: AuditTelemetryCollector | None,
    completion_path: str,
) -> None:
    state = runtime.backend_manager.get_state_snapshot()
    cfg = load_audit_config(state)
    criteria = state.get("audit_criteria")
    if not isinstance(criteria, dict):
        criteria = None
    telemetry = runtime._build_audit_telemetry(item, response, audit_collector)
    visible_context = runtime._audit_visible_context(cfg.context_window)
    evidence_path = runtime._write_audit_evidence(
        item,
        core_raw=core_raw,
        visible_text=visible_text,
        telemetry=telemetry,
        completion_path=completion_path,
        audit_criteria=criteria,
        visible_context=visible_context,
    )
    processor = AuditProcessor(
        cfg,
        backend_invoker=runtime.backend_manager.generate_ephemeral_response,
        timeout_s=runtime._audit_timeout_s(),
    )
    audit_result = await processor.process(
        request_id=item.request_id,
        source=item.source,
        user_request=item.prompt,
        core_raw=core_raw,
        telemetry=telemetry,
        audit_criteria=criteria,
        visible_context=visible_context,
        config=cfg,
        evidence_path=evidence_path,
        silent=True,
    )
    runtime._append_audit_transcript(
        item,
        core_raw=core_raw,
        visible_text=visible_text,
        telemetry=telemetry,
        audit_result=audit_result,
        completion_path=completion_path,
    )
    if (
        should_notify_audit_result(audit_result, cfg)
        and not item.silent
        and item.deliver_to_telegram
        and not runtime._should_buffer_during_transfer(item.request_id)
    ):
        await runtime.send_long_message(
            chat_id=item.chat_id,
            text=format_audit_report(audit_result),
            request_id=item.request_id,
            purpose="audit-report",
        )
