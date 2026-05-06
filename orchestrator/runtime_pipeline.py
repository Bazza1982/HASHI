from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class QueueItemStart:
    is_bridge_request: bool
    queued_at: datetime
    queue_wait_s: float


@dataclass(frozen=True)
class TurnPrompt:
    effective_prompt: str
    final_prompt: str
    extra_sections: list[tuple[str, str]]
    habit_ids: list[str]
    incremental: bool
    prompt_audit: dict[str, Any]


def begin_queue_item(runtime, item) -> QueueItemStart:
    if not item.silent:
        runtime.last_prompt = item
    is_bridge_request = item.source.startswith("bridge:") or item.source.startswith("bridge-transfer:")
    queued_at = datetime.fromisoformat(item.created_at)
    queue_wait_s = (datetime.now() - queued_at).total_seconds()
    runtime.logger.info(
        f"Processing {item.request_id} via {runtime.config.active_backend} "
        f"(source={item.source}, silent={item.silent}, prompt_len={len(item.prompt)}, "
        f"queue_wait_s={queue_wait_s:.2f})"
    )
    runtime.current_request_meta = {
        "request_id": item.request_id,
        "source": item.source,
        "summary": item.summary,
        "started_at": datetime.now().isoformat(),
    }
    runtime._mark_activity()
    runtime._log_maintenance(
        item,
        "processing",
        engine=runtime.config.active_backend,
        silent=item.silent,
        prompt_len=len(item.prompt),
        queue_wait_s=f"{queue_wait_s:.2f}",
    )
    runtime.is_generating = True
    return QueueItemStart(
        is_bridge_request=is_bridge_request,
        queued_at=queued_at,
        queue_wait_s=queue_wait_s,
    )


async def build_turn_prompt(runtime, item, *, is_bridge_request: bool) -> TurnPrompt:
    effective_prompt = runtime._consume_session_primer(item)
    habit_sections, habit_ids = runtime._build_habit_sections(item, effective_prompt)
    extra_sections = runtime._workzone_prompt_section() + habit_sections
    extra_sections += await runtime._build_pre_turn_context_sections(
        item,
        effective_prompt,
        is_bridge_request=is_bridge_request,
    )
    runtime.current_request_meta["habit_ids"] = habit_ids
    incremental = (
        runtime.backend_manager.agent_mode == "fixed"
        and hasattr(runtime.backend_manager.current_backend, "_session_id")
        and runtime.backend_manager.current_backend._session_id is not None
    )
    prompt_payload = runtime.context_assembler.build_prompt_payload(
        effective_prompt,
        runtime.config.active_backend,
        extra_sections=extra_sections,
        inject_memory=not item.skip_memory_injection,
        incremental=incremental,
    )
    final_prompt = prompt_payload["final_prompt"]
    prompt_audit = prompt_payload.get("audit", {})
    runtime._last_prompt_audit = prompt_audit
    runtime._thinking_chars_this_req = 0
    runtime._last_full_prompt_tokens = len(final_prompt) // 4
    return TurnPrompt(
        effective_prompt=effective_prompt,
        final_prompt=final_prompt,
        extra_sections=extra_sections,
        habit_ids=habit_ids,
        incremental=incremental,
        prompt_audit=prompt_audit,
    )
