"""Protocol ACK state helpers for Hashi Remote."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def ack_state_path(instance_id: str) -> Path:
    instance_key = str(instance_id or "hashi").strip().lower() or "hashi"
    return Path.home() / ".hashi-remote" / f"protocol_ack_{instance_key}.json"


def load_ack_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    messages = data.get("messages") if isinstance(data, dict) else {}
    return messages if isinstance(messages, dict) else {}


def save_ack_state(path: Path, messages: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"messages": messages}, indent=2, ensure_ascii=False), encoding="utf-8")


def record_protocol_ack(
    payload: dict[str, Any],
    *,
    local_instance: str,
    state_path: Path | None = None,
) -> dict[str, Any]:
    message_id = str((payload or {}).get("message_id") or "").strip()
    conversation_id = str((payload or {}).get("conversation_id") or "").strip()
    from_instance = str((payload or {}).get("from_instance") or "").strip().upper()
    to_instance = str((payload or {}).get("to_instance") or "").strip().upper()
    state = str((payload or {}).get("state") or "ack").strip() or "ack"
    if not all([message_id, conversation_id, from_instance, to_instance]):
        return {
            "ok": False,
            "code": "invalid_ack",
            "error": "message_id, conversation_id, from_instance, and to_instance are required",
        }
    normalized_local = str(local_instance or "").strip().upper()
    if normalized_local and to_instance != normalized_local:
        return {
            "ok": False,
            "code": "wrong_target_instance",
            "error": f"ack target {to_instance} does not match local instance {normalized_local}",
        }
    now = int(time.time())
    path = state_path or ack_state_path(to_instance)
    messages = load_ack_state(path)
    item = dict(messages.get(message_id) or {})
    item.update(
        {
            "message_id": message_id,
            "conversation_id": conversation_id,
            "from_instance": from_instance,
            "to_instance": to_instance,
            "state": state,
            "updated_at": now,
        }
    )
    item.setdefault("created_at", now)
    details = (payload or {}).get("details")
    if isinstance(details, dict):
        item["details"] = details
    messages[message_id] = item
    save_ack_state(path, messages)
    return {"ok": True, "message_id": message_id, "state": state}
