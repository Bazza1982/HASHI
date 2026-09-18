"""Durable Agent-level stop fences shared by replaceable Function services.

The fence is deliberately small: it does not schedule work or own Turn state.
It only gives existing queues, schedulers, and completion callbacks one durable
generation number with which to reject work that predates an explicit /stop.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


@dataclass(frozen=True)
class AgentStopFence:
    agent: str
    epoch: int = 0
    stopped_at: str = ""
    reason: str = ""
    receipt_id: str = ""
    request_id: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "epoch": self.epoch,
            "stopped_at": self.stopped_at,
            "reason": self.reason,
            "receipt_id": self.receipt_id,
            "request_id": self.request_id,
            "source": self.source,
        }


class AgentStopFenceStore:
    """SQLite-backed monotonic stop generations for one HASHI instance."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_stop_fences (
                    agent TEXT PRIMARY KEY,
                    epoch INTEGER NOT NULL,
                    stopped_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    receipt_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    source TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @staticmethod
    def _from_row(agent: str, row: sqlite3.Row | None) -> AgentStopFence:
        if row is None:
            return AgentStopFence(agent=agent)
        return AgentStopFence(
            agent=str(row["agent"]),
            epoch=max(0, int(row["epoch"])),
            stopped_at=str(row["stopped_at"] or ""),
            reason=str(row["reason"] or ""),
            receipt_id=str(row["receipt_id"] or ""),
            request_id=str(row["request_id"] or ""),
            source=str(row["source"] or ""),
        )

    def current(self, agent: str) -> AgentStopFence:
        name = str(agent or "").strip()
        if not name:
            raise ValueError("agent stop fence requires an agent name")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_stop_fences WHERE agent = ?",
                (name,),
            ).fetchone()
        return self._from_row(name, row)

    def advance(
        self,
        agent: str,
        *,
        reason: str = "user_stop",
        request_id: str = "",
        source: str = "",
    ) -> AgentStopFence:
        name = str(agent or "").strip()
        if not name:
            raise ValueError("agent stop fence requires an agent name")
        stopped_at = datetime.now(timezone.utc).isoformat()
        receipt_id = f"stop-{uuid4().hex[:16]}"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT epoch FROM agent_stop_fences WHERE agent = ?",
                (name,),
            ).fetchone()
            epoch = (int(row["epoch"]) if row is not None else 0) + 1
            conn.execute(
                """
                INSERT INTO agent_stop_fences (
                    agent, epoch, stopped_at, reason, receipt_id, request_id, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent) DO UPDATE SET
                    epoch = excluded.epoch,
                    stopped_at = excluded.stopped_at,
                    reason = excluded.reason,
                    receipt_id = excluded.receipt_id,
                    request_id = excluded.request_id,
                    source = excluded.source
                """,
                (
                    name,
                    epoch,
                    stopped_at,
                    str(reason or "user_stop"),
                    receipt_id,
                    str(request_id or ""),
                    str(source or ""),
                ),
            )
            conn.commit()
        return AgentStopFence(
            agent=name,
            epoch=epoch,
            stopped_at=stopped_at,
            reason=str(reason or "user_stop"),
            receipt_id=receipt_id,
            request_id=str(request_id or ""),
            source=str(source or ""),
        )


def stop_fence_path(bridge_home: str | Path) -> Path:
    return (
        Path(bridge_home).expanduser().resolve()
        / "state"
        / "runtime_control"
        / "agent_stop_fences.db"
    )


def store_for_runtime(runtime: Any) -> AgentStopFenceStore | None:
    global_config = getattr(runtime, "global_config", None)
    bridge_home = getattr(global_config, "bridge_home", None)
    if not bridge_home:
        return None
    path = stop_fence_path(bridge_home)
    cached = getattr(runtime, "_agent_stop_fence_store", None)
    if isinstance(cached, AgentStopFenceStore) and cached.path == path:
        return cached
    store = AgentStopFenceStore(path)
    runtime._agent_stop_fence_store = store
    return store


def current_runtime_epoch(runtime: Any) -> int:
    local_epoch = max(0, int(getattr(runtime, "_agent_stop_epoch", 0) or 0))
    store = store_for_runtime(runtime)
    if store is not None:
        local_epoch = max(local_epoch, store.current(getattr(runtime, "name", "")).epoch)
    runtime._agent_stop_epoch = local_epoch
    return local_epoch


def advance_runtime_fence(
    runtime: Any,
    *,
    reason: str = "user_stop",
    request_id: str = "",
    source: str = "",
) -> AgentStopFence:
    store = store_for_runtime(runtime)
    if store is not None:
        fence = store.advance(
            getattr(runtime, "name", ""),
            reason=reason,
            request_id=request_id,
            source=source,
        )
    else:
        epoch = max(0, int(getattr(runtime, "_agent_stop_epoch", 0) or 0)) + 1
        fence = AgentStopFence(
            agent=str(getattr(runtime, "name", "") or "unknown"),
            epoch=epoch,
            stopped_at=datetime.now(timezone.utc).isoformat(),
            reason=str(reason or "user_stop"),
            receipt_id=f"stop-{uuid4().hex[:16]}",
            request_id=str(request_id or ""),
            source=str(source or ""),
        )
    runtime._agent_stop_epoch = fence.epoch
    runtime._last_agent_stop_receipt = fence.to_dict()
    return fence


def record_predates_fence(
    fence: AgentStopFence,
    *,
    origin: Mapping[str, Any] | None,
    created_at: str = "",
) -> bool:
    if fence.epoch <= 0:
        return False
    payload = origin if isinstance(origin, Mapping) else {}
    raw_epoch = payload.get("agent_stop_epoch")
    try:
        if raw_epoch is not None:
            return int(raw_epoch) < fence.epoch
    except (TypeError, ValueError):
        return True
    if not created_at or not fence.stopped_at:
        return True
    try:
        created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        stopped = datetime.fromisoformat(str(fence.stopped_at).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if stopped.tzinfo is None:
            stopped = stopped.replace(tzinfo=timezone.utc)
        return created <= stopped
    except (TypeError, ValueError):
        return True


__all__ = [
    "AgentStopFence",
    "AgentStopFenceStore",
    "advance_runtime_fence",
    "current_runtime_epoch",
    "record_predates_fence",
    "stop_fence_path",
    "store_for_runtime",
]
