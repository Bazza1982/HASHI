"""Outbound protocol correlation registry helpers."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def outbound_state_path(instance_id: str) -> Path:
    instance_key = str(instance_id or "hashi").strip().lower() or "hashi"
    return Path.home() / ".hashi-remote" / f"protocol_outbound_{instance_key}.json"


def load_outbound_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    messages = data.get("messages") if isinstance(data, dict) else {}
    return messages if isinstance(messages, dict) else {}


def save_outbound_state(path: Path, messages: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        tmp_path.write_text(
            json.dumps({"messages": messages}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_path.replace(path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


_CORRELATION_IDENTITY_FIELDS = (
    "conversation_id",
    "from_instance",
    "from_agent",
    "to_instance",
    "to_agent",
)


def _normalized_identity_value(field: str, value: Any) -> str:
    normalized = str(value or "").strip()
    if field in {"from_instance", "to_instance"}:
        return normalized.upper()
    if field in {"from_agent", "to_agent"}:
        return normalized.casefold()
    return normalized


def register_outbound_correlation(
    payload: dict[str, Any],
    *,
    local_instance: str,
    state_path: Path | None = None,
) -> dict[str, Any]:
    message_id = str((payload or {}).get("message_id") or "").strip()
    conversation_id = str((payload or {}).get("conversation_id") or "").strip()
    from_instance = str((payload or {}).get("from_instance") or "").strip().upper()
    from_agent = str((payload or {}).get("from_agent") or "").strip().lower()
    to_instance = str((payload or {}).get("to_instance") or "").strip().upper()
    to_agent = str((payload or {}).get("to_agent") or "").strip().lower()
    if not all([message_id, conversation_id, from_instance, from_agent, to_instance, to_agent]):
        return {
            "ok": False,
            "code": "invalid_outbound_correlation",
            "error": "message_id, conversation_id, from_instance, from_agent, to_instance, and to_agent are required",
        }
    normalized_local = str(local_instance or "").strip().upper()
    if normalized_local and from_instance != normalized_local:
        return {
            "ok": False,
            "code": "wrong_source_instance",
            "error": f"outbound correlation source {from_instance} does not match local instance {normalized_local}",
        }
    now = int(time.time())
    path = state_path or outbound_state_path(from_instance)
    messages = load_outbound_state(path)
    item = dict(messages.get(message_id) or {})
    if item and any(
        _normalized_identity_value(field, item.get(field))
        != _normalized_identity_value(field, (payload or {}).get(field))
        for field in _CORRELATION_IDENTITY_FIELDS
    ):
        return {
            "ok": False,
            "code": "outbound_correlation_conflict",
            "error": "message_id is already bound to a different HChat route",
        }
    item.update(
        {
            "message_id": message_id,
            "conversation_id": conversation_id,
            "from_instance": from_instance,
            "from_agent": from_agent,
            "to_instance": to_instance,
            "to_agent": to_agent,
            "state": str((payload or {}).get("state") or "sending").strip() or "sending",
            "updated_at": now,
        }
    )
    item.setdefault("created_at", now)
    result = (payload or {}).get("result")
    if isinstance(result, dict):
        item["last_result"] = result
    messages[message_id] = item
    save_outbound_state(path, messages)
    return {
        "ok": True,
        "message_id": message_id,
        "state": item["state"],
        "correlation_state": "registered_outbound",
    }


def get_outbound_correlation(
    message_id: str,
    *,
    local_instance: str,
    state_path: Path | None = None,
) -> dict[str, Any] | None:
    """Read one correlation from the local instance's durable registry."""

    normalized_message_id = str(message_id or "").strip()
    normalized_instance = str(local_instance or "").strip().upper()
    if not normalized_message_id or not normalized_instance:
        return None
    path = state_path or outbound_state_path(normalized_instance)
    item = load_outbound_state(path).get(normalized_message_id)
    return dict(item) if isinstance(item, dict) else None
