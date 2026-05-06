from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from orchestrator.wrapper_mode import (
    WrapperProcessor,
    load_wrapper_config,
    passthrough_result,
    should_wrap_source,
)


def wrapper_enabled(runtime: Any) -> bool:
    return getattr(runtime.backend_manager, "agent_mode", "flex") == "wrapper"


def wrapper_timeout_s(runtime: Any) -> float:
    try:
        value = float((runtime.config.extra or {}).get("wrapper_timeout_s", 30.0))
    except (TypeError, ValueError):
        value = 30.0
    return value if value > 0 else 30.0


def wrapper_visible_context(runtime: Any, context_window: int) -> list[dict[str, str]]:
    if context_window <= 0:
        return []
    try:
        rounds = runtime.handoff_builder.get_recent_rounds(max_rounds=context_window)
    except Exception as exc:
        runtime.logger.warning(f"_wrapper_visible_context: get_recent_rounds failed: {exc}")
        return []
    context: list[dict[str, str]] = []
    for round_entries in rounds:
        for entry in round_entries:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            context.append(
                {
                    "role": str(entry.get("role") or "unknown"),
                    "text": text,
                    "source": str(entry.get("source") or ""),
                }
            )
    return context


def wrapper_audit_fields(runtime: Any, wrapper_result) -> dict[str, Any]:
    return {
        "wrapper_mode": wrapper_enabled(runtime),
        "wrapper_used": bool(getattr(wrapper_result, "wrapper_used", False)),
        "wrapper_failed": bool(getattr(wrapper_result, "wrapper_failed", False)),
        "wrapper_latency_ms": round(float(getattr(wrapper_result, "latency_ms", 0.0) or 0.0), 3),
        "wrapper_fallback_reason": getattr(wrapper_result, "fallback_reason", None),
        "wrapper_final_chars": len(getattr(wrapper_result, "final_text", "") or ""),
    }


def wrapper_listener_fields(core_raw: str, visible_text: str, wrapper_result) -> dict[str, Any]:
    return {
        "core_raw": core_raw,
        "visible_text": visible_text,
        "wrapper_used": bool(getattr(wrapper_result, "wrapper_used", False)),
        "wrapper_failed": bool(getattr(wrapper_result, "wrapper_failed", False)),
        "wrapper_fallback_reason": getattr(wrapper_result, "fallback_reason", None),
    }


def core_memory_assistant_text(runtime: Any, core_raw: str, visible_text: str, wrapper_result) -> str:
    if wrapper_enabled(runtime) and (
        bool(getattr(wrapper_result, "wrapper_used", False))
        or bool(getattr(wrapper_result, "wrapper_failed", False))
    ):
        return core_raw or visible_text
    return visible_text


def append_core_transcript(
    runtime: Any,
    item: Any,
    *,
    core_raw: str,
    visible_text: str,
    completion_path: str,
    wrapper_result,
) -> None:
    path = getattr(runtime, "core_transcript_log_path", None) or (runtime.workspace_dir / "core_transcript.jsonl")
    entry = {
        "role": "assistant_core",
        "text": core_raw or "",
        "visible_text": visible_text or "",
        "source": item.source,
        "request_id": item.request_id,
        "summary": item.summary,
        "completion_path": completion_path,
        "backend": runtime.config.active_backend,
        "wrapper_used": bool(getattr(wrapper_result, "wrapper_used", False)),
        "wrapper_failed": bool(getattr(wrapper_result, "wrapper_failed", False)),
        "wrapper_fallback_reason": getattr(wrapper_result, "fallback_reason", None),
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        runtime.error_logger.warning(f"Failed to append core transcript: {exc}")


async def send_wrapper_polishing_placeholder(runtime: Any, item: Any):
    if item.silent or not item.deliver_to_telegram or not runtime.telegram_connected:
        return None
    if not should_wrap_source(item.source):
        return None
    bot = getattr(getattr(runtime, "app", None), "bot", None)
    if bot is None or not hasattr(bot, "send_message"):
        return None
    try:
        return await bot.send_message(
            chat_id=item.chat_id,
            text="✨ Polishing the final voice...",
        )
    except Exception as exc:
        runtime.telegram_logger.warning(f"Failed to send wrapper polishing placeholder: {exc}")
        return None


async def delete_wrapper_polishing_placeholder(runtime: Any, item: Any, placeholder) -> None:
    if placeholder is None:
        return
    bot = getattr(getattr(runtime, "app", None), "bot", None)
    message_id = getattr(placeholder, "message_id", None)
    if bot is None or message_id is None or not hasattr(bot, "delete_message"):
        return
    try:
        await bot.delete_message(chat_id=item.chat_id, message_id=message_id)
    except Exception:
        pass


async def apply_wrapper_to_visible_text(runtime: Any, item: Any, visible_text: str):
    if not wrapper_enabled(runtime):
        return visible_text, passthrough_result(visible_text, fallback_reason="wrapper_mode_disabled")

    state = runtime.backend_manager.get_state_snapshot()
    cfg = load_wrapper_config(state)
    slots = state.get("wrapper_slots")
    if not isinstance(slots, dict):
        slots = None
    processor = WrapperProcessor(
        cfg,
        backend_invoker=runtime.backend_manager.generate_ephemeral_response,
        timeout_s=wrapper_timeout_s(runtime),
    )

    stop_wrapper_typing = None
    wrapper_typing_task = None
    wrapper_placeholder = await runtime._send_wrapper_polishing_placeholder(item)
    if not item.silent and item.deliver_to_telegram and runtime.telegram_connected:
        stop_wrapper_typing = asyncio.Event()
        wrapper_typing_task = asyncio.create_task(runtime.typing_loop(item.chat_id, stop_wrapper_typing))
    try:
        wrapper_result = await processor.process(
            request_id=item.request_id,
            source=item.source,
            core_raw=visible_text,
            user_request=item.prompt,
            visible_context=runtime._wrapper_visible_context(cfg.context_window),
            wrapper_slots=slots,
            config=cfg,
            silent=True,
        )
    finally:
        if stop_wrapper_typing and wrapper_typing_task:
            stop_wrapper_typing.set()
            await wrapper_typing_task
        await runtime._delete_wrapper_polishing_placeholder(item, wrapper_placeholder)

    return wrapper_result.final_text, wrapper_result


def wrapper_verbose_excerpt(text: str, *, limit: int = 1800) -> str:
    value = text or ""
    if len(value) <= limit:
        return value
    head = value[: limit - 180].rstrip()
    tail = value[-140:].lstrip()
    return f"{head}\n\n... [truncated for verbose display] ...\n\n{tail}"


def format_wrapper_verbose_trace(runtime: Any, core_raw: str, visible_text: str, wrapper_result) -> str:
    status = "used" if bool(getattr(wrapper_result, "wrapper_used", False)) else "bypassed"
    if bool(getattr(wrapper_result, "wrapper_failed", False)):
        status = "failed"
    fallback_reason = getattr(wrapper_result, "fallback_reason", None)
    latency_ms = round(float(getattr(wrapper_result, "latency_ms", 0.0) or 0.0), 1)
    lines = [
        "🔍 Wrapper verbose trace",
        f"- Status: `{status}`",
        f"- Latency: `{latency_ms}ms`",
    ]
    if fallback_reason:
        lines.append(f"- Fallback: `{fallback_reason}`")
    lines.extend(
        [
            "",
            "**Core output**",
            "```text",
            runtime._wrapper_verbose_excerpt(core_raw),
            "```",
            "",
            "**Wrapper final output**",
            "```text",
            runtime._wrapper_verbose_excerpt(visible_text),
            "```",
        ]
    )
    return "\n".join(lines)


async def send_wrapper_verbose_trace(
    runtime: Any,
    item: Any,
    core_raw: str,
    visible_text: str,
    wrapper_result,
) -> None:
    if not runtime._verbose:
        return
    if item.silent or not item.deliver_to_telegram:
        return
    if not bool(getattr(wrapper_result, "wrapper_used", False)) and not bool(getattr(wrapper_result, "wrapper_failed", False)):
        return
    await runtime.send_long_message(
        chat_id=item.chat_id,
        text=runtime._format_wrapper_verbose_trace(core_raw, visible_text, wrapper_result),
        request_id=item.request_id,
        purpose="wrapper-verbose",
    )
