from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.bridge_memory import SysPromptManager
from orchestrator.parked_topics import ParkedTopicStore
from orchestrator.workzone import load_workzone
from tools.token_tracker import get_summary

SYS_PREVIEW_CHARS = 70


def _usage_period(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    input_tokens = int(value.get("input", 0) or 0)
    output_tokens = int(value.get("output", 0) or 0)
    thinking_tokens = int(value.get("thinking", 0) or 0)
    return {
        "input": input_tokens,
        "output": output_tokens,
        "thinking": thinking_tokens,
        "total": int(
            value.get("total_tokens")
            if value.get("total_tokens") is not None
            else input_tokens + output_tokens + thinking_tokens
        ),
        "cost_usd": float(value.get("cost_usd", 0.0) or 0.0),
        "unknown_cost_requests": int(value.get("unknown_cost_requests", 0) or 0),
        "requests": int(value.get("requests", 0) or 0),
    }


def _usage_overview(workspace_dir: Path, session_id: str | None) -> dict[str, Any]:
    summary = get_summary(workspace_dir, session_id=session_id)
    by_model = [
        {"model": str(model), **(_usage_period(data) or {})}
        for model, data in sorted(
            summary.get("by_model", {}).items(),
            key=lambda item: -float(item[1].get("cost_usd", 0.0) or 0.0),
        )
    ]
    return {
        "all_time": _usage_period(summary.get("all_time")),
        "session": _usage_period(summary.get("session")),
        "by_model": by_model,
    }


def _system_prompt_overview(manager: Any) -> dict[str, Any]:
    slots = []
    for item in manager.list_slots():
        text = str(item.get("text") or "")
        active = bool(item.get("active"))
        preview = text[:SYS_PREVIEW_CHARS].strip()
        if len(text) > SYS_PREVIEW_CHARS:
            preview += "…"
        slots.append(
            {
                "slot": str(item.get("slot") or ""),
                "active": active,
                "configured": bool(text),
                "state": "on" if active else ("off" if text else "empty"),
                "preview": preview,
                "characters": len(text),
            }
        )
    return {
        "active_count": sum(int(item["active"]) for item in slots),
        "configured_count": sum(int(item["configured"]) for item in slots),
        "total_count": len(slots),
        "slots": slots,
    }


def _parked_topics_overview(store: Any) -> dict[str, Any]:
    topics = []
    for item in store.list_topics():
        followup = item.get("followup") if isinstance(item.get("followup"), dict) else {}
        topics.append(
            {
                "slot": int(item.get("slot_id", 0) or 0),
                "title": str(item.get("title") or ""),
                "summary_short": str(item.get("summary_short") or ""),
                "followup": {
                    "status": str(followup.get("status") or ""),
                    "attempts": int(followup.get("attempts", 0) or 0),
                    "max_attempts": int(followup.get("max_attempts", 0) or 0),
                    "next_at": followup.get("next_at"),
                },
            }
        )
    return {"count": len(topics), "topics": topics}


def build_agent_overview(
    *,
    metadata: dict[str, Any],
    workspace_dir: Path,
    runtime: Any | None = None,
) -> dict[str, Any]:
    """Project existing HASHI state for one read-only Workbench panel."""
    workspace_dir = Path(workspace_dir)
    prompt_manager = getattr(runtime, "sys_prompt_manager", None) or SysPromptManager(workspace_dir)
    parked_store = getattr(runtime, "parked_topics", None) or ParkedTopicStore(workspace_dir)
    session_id = str(getattr(runtime, "session_id_dt", "") or "") or None
    workzone = load_workzone(workspace_dir)

    return {
        "agent": {
            "id": str(metadata.get("id") or metadata.get("name") or ""),
            "name": str(metadata.get("name") or metadata.get("id") or ""),
            "display_name": str(metadata.get("display_name") or metadata.get("name") or ""),
            "status": str(metadata.get("status") or "offline"),
            "online": bool(metadata.get("online")),
        },
        "workzone": {
            "active": workzone is not None,
            "path": str(workzone) if workzone is not None else None,
        },
        "usage": _usage_overview(workspace_dir, session_id),
        "system_prompts": _system_prompt_overview(prompt_manager),
        "parked_topics": _parked_topics_overview(parked_store),
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }
