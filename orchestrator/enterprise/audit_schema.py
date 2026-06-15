from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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
    def __init__(self, *, enabled: bool = False):
        self.enabled = enabled

    def append(self, event: AuditEvent) -> None:
        if not self.enabled:
            return
        # Stub: keep contract stable for later integration with unified ledger.
        _ = event.to_dict()
