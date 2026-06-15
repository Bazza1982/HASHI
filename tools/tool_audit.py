from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

_WRITE_LOCK = threading.Lock()
_SECRET_PATTERN = re.compile(r"(?i)(api[_-]?key|token|password|passwd|secret|bearer)\s*[:=]\s*\S+")
_MAX_VALUE_CHARS = 800


def default_audit_path(workspace_dir: Path) -> Path:
    return Path(workspace_dir) / "tool_action_audit.jsonl"


def sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): sanitize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_value(item) for item in value]
    if isinstance(value, str):
        cleaned = _SECRET_PATTERN.sub(r"\1=[redacted]", value)
        if len(cleaned) > _MAX_VALUE_CHARS:
            return cleaned[:_MAX_VALUE_CHARS] + "...[truncated]"
        return cleaned
    return value


def build_tool_audit_record(
    *,
    tool_name: str,
    tool_call_id: str,
    arguments: dict,
    output: str,
    is_error: bool,
    duration_ms: int,
    audit_context: dict | None = None,
    ts: float | None = None,
) -> dict[str, Any]:
    context = dict(audit_context or {})
    return {
        "ts": time.time() if ts is None else ts,
        "kind": "tool_action",
        "tool_name": str(tool_name or ""),
        "tool_call_id": str(tool_call_id or ""),
        "agent": context.get("agent_name"),
        "workspace_dir": context.get("workspace_dir"),
        "safety_mode": context.get("safety_mode"),
        "status": "failed" if is_error else "success",
        "is_error": bool(is_error),
        "duration_ms": max(0, int(duration_ms)),
        "args_redacted": sanitize_value(arguments or {}),
        "output_snippet": sanitize_value(str(output or "")[:_MAX_VALUE_CHARS]),
    }


def append_tool_audit_record(path: Path, record: dict[str, Any]) -> Path:
    audit_path = Path(path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(sanitize_value(record), ensure_ascii=False)
    with _WRITE_LOCK:
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    return audit_path


def record_tool_action(
    *,
    workspace_dir: Path,
    tool_name: str,
    tool_call_id: str,
    arguments: dict,
    output: str,
    is_error: bool,
    duration_ms: int,
    audit_context: dict | None = None,
) -> None:
    try:
        append_tool_audit_record(
            default_audit_path(workspace_dir),
            build_tool_audit_record(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                arguments=arguments,
                output=output,
                is_error=is_error,
                duration_ms=duration_ms,
                audit_context=audit_context,
            ),
        )
    except Exception:
        pass
