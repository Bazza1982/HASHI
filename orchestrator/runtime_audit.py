from __future__ import annotations

import asyncio
import html
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
    visible_audit_criteria,
)
from orchestrator.runtime_common import QueuedRequest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def audit_enabled(runtime: Any) -> bool:
    return getattr(runtime.backend_manager, "agent_mode", "flex") == "audit"


def audit_timeout_s(runtime: Any) -> float:
    state = runtime.backend_manager.get_state_snapshot()
    cfg = load_audit_config(state)
    return cfg.timeout_s


def audit_core_model_choices(runtime: Any) -> list[tuple[str, str, str, str]]:
    return runtime._filter_allowed_model_choices(
        [
            ("codex_gpt55", "Codex GPT-5.5", "codex-cli", "gpt-5.5"),
            ("codex_gpt54", "Codex GPT-5.4", "codex-cli", "gpt-5.4"),
            ("codex_gpt53", "Codex GPT-5.3", "codex-cli", "gpt-5.3-codex"),
            ("claude_opus", "Claude Opus 4.7", "claude-cli", "claude-opus-4-7"),
            ("claude_opus_46", "Claude Opus 4.6", "claude-cli", "claude-opus-4-6"),
            ("claude_sonnet", "Claude Sonnet 4.6", "claude-cli", "claude-sonnet-4-6"),
            ("gemini_pro", "Gemini Pro", "gemini-cli", "gemini-2.5-pro"),
            ("gemini_flash", "Gemini Flash", "gemini-cli", "gemini-2.5-flash"),
            ("deepseek_pro", "DeepSeek Pro", "deepseek-api", "deepseek-v4-pro"),
            ("deepseek_reasoner", "DeepSeek Reasoner", "deepseek-api", "deepseek-reasoner"),
            ("or_sonnet", "OR Sonnet 4.6", "openrouter-api", "anthropic/claude-sonnet-4.6"),
            ("or_opus", "OR Opus 4.6", "openrouter-api", "anthropic/claude-opus-4.6"),
            ("or_deepseek", "OR DeepSeek", "openrouter-api", "deepseek/deepseek-v3.2-exp"),
            ("ollama_gemma", "Ollama Gemma", "ollama-api", "gemma4:26b"),
            ("ollama_qwen", "Ollama Qwen", "ollama-api", "qwen3:32b"),
        ]
    )


def audit_auditor_model_choices(runtime: Any) -> list[tuple[str, str, str, str]]:
    return runtime._filter_allowed_model_choices(
        [
            ("claude_opus", "Claude Opus 4.7", "claude-cli", "claude-opus-4-7"),
            ("claude_opus_46", "Claude Opus 4.6", "claude-cli", "claude-opus-4-6"),
            ("claude_sonnet", "Claude Sonnet 4.6", "claude-cli", "claude-sonnet-4-6"),
            ("or_sonnet", "OR Sonnet 4.6", "openrouter-api", "anthropic/claude-sonnet-4.6"),
            ("or_opus", "OR Opus 4.6", "openrouter-api", "anthropic/claude-opus-4.6"),
            ("codex_gpt55", "Codex GPT-5.5", "codex-cli", "gpt-5.5"),
            ("codex_gpt54", "Codex GPT-5.4", "codex-cli", "gpt-5.4"),
            ("deepseek_pro", "DeepSeek Pro", "deepseek-api", "deepseek-v4-pro"),
            ("deepseek_reasoner", "DeepSeek Reasoner", "deepseek-api", "deepseek-reasoner"),
            ("gemini_pro", "Gemini Pro", "gemini-cli", "gemini-2.5-pro"),
            ("gemini_flash", "Gemini Flash", "gemini-cli", "gemini-2.5-flash"),
        ]
    )


def audit_choice_by_id(runtime: Any, target: str, choice_id: str) -> tuple[str, str, str, str] | None:
    choices = runtime._audit_core_model_choices() if target == "core" else runtime._audit_auditor_model_choices()
    return next((choice for choice in choices if choice[0] == choice_id), None)


def audit_model_keyboard(runtime: Any, cfg: Any, *, target: str) -> InlineKeyboardMarkup:
    choices = runtime._audit_core_model_choices() if target == "core" else runtime._audit_auditor_model_choices()
    current_backend = cfg.core_backend if target == "core" else cfg.audit_backend
    current_model = cfg.core_model if target == "core" else cfg.audit_model
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for choice_id, label, backend, model in choices:
        active = current_backend == backend and current_model == model
        row.append(
            InlineKeyboardButton(
                f"✅ {label}" if active else label,
                callback_data=f"acfg:{target}id:{choice_id}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton("Core model", callback_data="acfg:menu:core"),
            InlineKeyboardButton("Audit model", callback_data="acfg:menu:auditmodel"),
        ]
    )
    rows.append([InlineKeyboardButton("Audit config", callback_data="acfg:menu:audit")])
    return InlineKeyboardMarkup(rows)


def audit_core_text(cfg: Any) -> str:
    return (
        "Audit core model:\n"
        f"• Backend: `{cfg.core_backend}`\n"
        f"• Model: `{cfg.core_model}`\n\n"
        "This is the model that does the actual work. Tap a provider/model button below, or type:\n"
        "`/core backend=claude-cli model=claude-sonnet-4-6`\n"
        "`/core backend=codex-cli model=gpt-5.5`\n"
        "`/core backend=deepseek-api model=deepseek-v4-pro`"
    )


def audit_auditor_text(cfg: Any) -> str:
    return (
        "Audit model:\n"
        f"• Backend: `{cfg.audit_backend}`\n"
        f"• Model: `{cfg.audit_model}`\n"
        f"• Delivery: `{cfg.delivery}`\n"
        f"• Severity threshold: `{cfg.severity_threshold}`\n\n"
        "This model reviews the core model's observable thinking/actions/output. "
        "Use a strong reviewer model here. Tap a button below, or type:\n"
        "`/audit model backend=claude-cli model=claude-opus-4-7`\n"
        "`/audit model backend=claude-cli model=claude-sonnet-4-6`\n"
        "`/audit model backend=openrouter-api model=anthropic/claude-sonnet-4.6`"
    )


def audit_config_keyboard(cfg: Any) -> InlineKeyboardMarkup:
    delivery_row = [
        InlineKeyboardButton(
            f"{'✅ ' if cfg.delivery == value else ''}{label}",
            callback_data=f"acfg:delivery:{value}",
        )
        for value, label in (
            ("always", "Always report"),
            ("issues_only", "Issues only"),
            ("silent", "Silent log"),
        )
    ]
    threshold_row = [
        InlineKeyboardButton(
            f"{'✅ ' if cfg.severity_threshold == value else ''}{label}",
            callback_data=f"acfg:threshold:{value}",
        )
        for value, label in (
            ("low", "Low+"),
            ("medium", "Medium+"),
            ("high", "High+"),
            ("critical", "Critical"),
        )
    ]
    return InlineKeyboardMarkup(
        [
            delivery_row,
            threshold_row,
            [
                InlineKeyboardButton("Core model", callback_data="acfg:menu:core"),
                InlineKeyboardButton("Audit model", callback_data="acfg:menu:auditmodel"),
            ],
            [InlineKeyboardButton("Refresh", callback_data="acfg:menu:audit")],
        ]
    )


def audit_block_with(
    cfg: Any,
    *,
    delivery: str | None = None,
    severity_threshold: str | None = None,
    timeout_s: float | None = None,
    backend: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    return {
        "backend": backend or cfg.audit_backend,
        "model": model or cfg.audit_model,
        "context_window": cfg.context_window,
        "delivery": delivery or cfg.delivery,
        "severity_threshold": severity_threshold or cfg.severity_threshold,
        "fail_policy": cfg.fail_policy,
        "timeout_s": cfg.timeout_s if timeout_s is None else timeout_s,
    }


def audit_status_text(state: dict, criteria: dict) -> str:
    cfg = load_audit_config(state)
    visible_criteria = visible_audit_criteria(criteria)
    lines = [
        "Audit mode configuration:",
        f"• Core: <code>{html.escape(cfg.core_backend)} / {html.escape(cfg.core_model)}</code>",
        f"• Audit: <code>{html.escape(cfg.audit_backend)} / {html.escape(cfg.audit_model)}</code>",
        f"• Delivery: <code>{cfg.delivery}</code>",
        f"• Severity threshold: <code>{cfg.severity_threshold}</code>",
        f"• Timeout: <code>{cfg.timeout_s:g}s</code>",
        "",
        "Default testing posture is <code>always</code> delivery and <code>low</code> threshold.",
        "Tap buttons below to change audit visibility/sensitivity, core model, or audit model.",
        "Use <code>/audit model backend=&lt;backend&gt; model=&lt;model&gt;</code> to set the audit model.",
        "Use <code>/audit delivery &lt;always|issues_only|silent&gt;</code> and <code>/audit threshold &lt;low|medium|high|critical&gt;</code> for text control.",
        "Use <code>/audit set &lt;slot&gt; &lt;text&gt;</code> to edit audit criteria.",
        "",
        "Audit criteria:",
    ]
    if visible_criteria:
        for key in sorted(visible_criteria, key=lambda value: (not str(value).isdigit(), int(value) if str(value).isdigit() else str(value))):
            lines.append(f"• <code>{html.escape(str(key))}</code>: {html.escape(str(visible_criteria[key]))}")
    else:
        lines.append("• default risk sensors")
    return "\n".join(lines)


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
