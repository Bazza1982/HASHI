"""Generation-bound restart receipts for the isolated HER live harness."""

from __future__ import annotations

import json
from pathlib import Path


def runtime_start_marker(session_state_path: Path) -> str | None:
    """Return the persisted runtime start marker, or ``None`` if unavailable."""

    try:
        payload = json.loads(session_state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    marker = str(payload.get("last_started_at") or "").strip()
    return marker or None


def restart_receipt_ready(
    *,
    previous_started_at: str | None,
    current_started_at: str | None,
    online: bool,
    idle: bool,
) -> bool:
    """Accept readiness only from a runtime generation newer than the request target."""

    return bool(
        previous_started_at
        and current_started_at
        and current_started_at != previous_started_at
        and online
        and idle
    )
