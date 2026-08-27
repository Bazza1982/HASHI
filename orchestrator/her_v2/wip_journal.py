"""Crash-safe, model-independent HER v2 work-in-progress context journal."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FORMAT = "her-v2-wip-journal-v1"
CONTEXT_HEADER = "[HASHI HER v2 WIP context from interrupted earlier turn(s)]"
CONTEXT_NOTICE = (
    "Earlier HER v2 work did not reach a completed Ledger state. The WIP Journal "
    "below preserves observable progress and status. It may be unrelated to the "
    "current request. Do not continue it by default; confirm with the user when "
    "whether to continue matters."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class WIPJournal:
    """Append observable turn records until a later Ledger completes."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _append(self, record: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(dict(record), ensure_ascii=False, sort_keys=True) + "\n"
        needs_separator = self.path.exists() and self.path.stat().st_size > 0
        if needs_separator:
            with self.path.open("rb") as existing:
                existing.seek(-1, os.SEEK_END)
                needs_separator = existing.read(1) != b"\n"
        with self.path.open("a", encoding="utf-8") as handle:
            if needs_separator:
                handle.write("\n")
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self.path.chmod(0o600)

    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self.path.exists():
                return []
            result: list[dict[str, Any]] = []
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, Mapping) and value.get("format") == FORMAT:
                    result.append(dict(value))
            return result

    def activity_summary(self) -> dict[str, int]:
        """Return non-content metrics suitable for lifecycle audit records."""
        with self._lock:
            size_bytes = self.path.stat().st_size if self.path.exists() else 0
            return {
                "record_count": len(self.records()),
                "size_bytes": int(size_bytes),
            }

    def begin_turn(self, *, request_id: str, prompt: str) -> str:
        """Return prior WIP context, then append the new turn boundary."""
        with self._lock:
            prior = self.records()
            self._append(
                {
                    "format": FORMAT,
                    "recorded_at": _now(),
                    "kind": "turn_received",
                    "request_id": str(request_id),
                    "prompt": str(prompt),
                }
            )
        return self.render_context(prior)

    def append_audit(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self._append(
                {
                    "format": FORMAT,
                    "recorded_at": _now(),
                    "kind": "her_v2_audit",
                    "audit": dict(record),
                }
            )

    @staticmethod
    def render_context(records: list[Mapping[str, Any]]) -> str:
        if not records:
            return ""
        serialized = "\n".join(
            json.dumps(dict(record), ensure_ascii=False, sort_keys=True)
            for record in records
        )
        return f"{CONTEXT_HEADER}\n{CONTEXT_NOTICE}\n\n{serialized}\n[End HER v2 WIP context]"

    def clear_completed(self) -> None:
        """Clear only after the completed Ledger is already durable."""
        with self._lock:
            if not self.path.exists():
                return
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self.path.chmod(0o600)


__all__ = ["CONTEXT_HEADER", "CONTEXT_NOTICE", "FORMAT", "WIPJournal"]
