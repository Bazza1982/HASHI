from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class AuditEvent:
    event_type: str
    actor_id: str | int | None
    action: str
    status: str
    ts: str = None
    context: Mapping[str, Any] | None = None

    def __post_init__(self):
        if self.ts is None:
            object.__setattr__(self, "ts", _utc_now_iso())

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "event_type": self.event_type,
            "actor_id": self.actor_id,
            "action": self.action,
            "status": self.status,
            "context": dict(self.context or {}),
        }


class AuditEventWriter:
    def __init__(self, *, enabled: bool = False, jsonl_path: Path | str | None = None):
        self.enabled = enabled
        self.jsonl_path = Path(jsonl_path) if jsonl_path is not None else None
        self._lock = threading.Lock()

    def append(self, event: AuditEvent) -> None:
        if not self.enabled:
            return
        if self.jsonl_path is None:
            return
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
        with self._lock:
            with self.jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
