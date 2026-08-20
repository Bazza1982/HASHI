"""Minimal HER v2 execution ledger and restart reconciliation."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .lifecycle import LifecycleMachine, LifecycleViolation
from .models import LifecycleState, TERMINAL_STATES, TriageClassification


class LedgerInvariantError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class ExecutionLedger:
    turn_id: str
    request_ref: str
    goal_ref: str
    status: LifecycleState = LifecycleState.RECEIVED
    classification: TriageClassification | None = None
    plan_id: str | None = None
    last_update: str = field(default_factory=utc_now)
    log_refs: list[str] = field(default_factory=list)
    terminal_reason: str | None = None
    format: str = "her-v2-ledger-v1"

    def __post_init__(self) -> None:
        if not self.turn_id.strip() or not self.request_ref.strip() or not self.goal_ref.strip():
            raise LedgerInvariantError("turn, request, and goal references are required")
        self._machine = LifecycleMachine(self.status)

    def _touch(self) -> None:
        self.last_update = utc_now()

    def record_triage(self, classification: TriageClassification) -> None:
        if self.classification is not None:
            raise LedgerInvariantError("Triage classification is immutable once recorded")
        if self.status is not LifecycleState.RECEIVED:
            raise LedgerInvariantError("Triage may only be recorded from RECEIVED")
        self.classification = classification
        self.transition(LifecycleState.TRIAGED)

    def assert_classification(self, classification: TriageClassification) -> None:
        if self.classification != classification:
            raise LedgerInvariantError(
                "downstream stage attempted to change the immutable Triage classification"
            )

    def transition(
        self, requested: LifecycleState, *, terminal_reason: str | None = None
    ) -> None:
        try:
            self._machine.transition(requested)
        except LifecycleViolation as exc:
            self.status = self._machine.state
            self.terminal_reason = str(exc)
            self._touch()
            raise
        self.status = self._machine.state
        if self.status in TERMINAL_STATES:
            self.terminal_reason = terminal_reason
        self._touch()

    def activate_plan(self, plan_id: str, *, replacement: bool = False) -> None:
        value = str(plan_id or "").strip()
        if not value:
            raise LedgerInvariantError("plan reference is required")
        if replacement:
            if self.status is not LifecycleState.REPLANNING:
                raise LedgerInvariantError(
                    "only Replanning may replace the active plan"
                )
            if self.plan_id == value:
                raise LedgerInvariantError("a Replan must create a new plan version")
        elif self.status is not LifecycleState.PLANNED or self.plan_id is not None:
            raise LedgerInvariantError(
                "the initial plan may only be activated once in PLANNED"
            )
        self.plan_id = value
        self._touch()

    def add_log_ref(self, log_ref: str) -> None:
        value = str(log_ref or "").strip()
        if not value or value in self.log_refs:
            return
        self.log_refs.append(value)
        # The ledger is a compact control record, not a second audit database.
        if len(self.log_refs) > 32:
            self.log_refs[:] = self.log_refs[-32:]
        self._touch()

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "turn_id": self.turn_id,
            "request_ref": self.request_ref,
            "goal_ref": self.goal_ref,
            "status": self.status.value,
            "classification": self.classification.value if self.classification else None,
            "plan_id": self.plan_id,
            "last_update": self.last_update,
            "log_refs": list(self.log_refs),
            "terminal_reason": self.terminal_reason,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExecutionLedger":
        try:
            status = LifecycleState(str(raw["status"]))
            classification_raw = raw.get("classification")
            classification = (
                TriageClassification(str(classification_raw))
                if classification_raw
                else None
            )
            ledger = cls(
                turn_id=str(raw["turn_id"]),
                request_ref=str(raw["request_ref"]),
                goal_ref=str(raw["goal_ref"]),
                status=status,
                classification=classification,
                plan_id=(str(raw["plan_id"]) if raw.get("plan_id") else None),
                last_update=str(raw.get("last_update") or utc_now()),
                log_refs=[str(item) for item in raw.get("log_refs") or []],
                terminal_reason=(
                    str(raw["terminal_reason"])
                    if raw.get("terminal_reason") is not None
                    else None
                ),
                format=str(raw.get("format") or "her-v2-ledger-v1"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerInvariantError("invalid HER v2 ledger payload") from exc
        if ledger.classification is None and ledger.status is not LifecycleState.RECEIVED:
            raise LedgerInvariantError("a post-Triage ledger requires a classification")
        return ledger


_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")


class LedgerStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, turn_id: str) -> Path:
        safe = _SAFE_ID.sub("_", str(turn_id)).strip("._")
        if not safe:
            raise LedgerInvariantError("turn identifier cannot form a safe filename")
        return self.root / f"{safe}.json"

    def save(self, ledger: ExecutionLedger) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path(ledger.turn_id)
        payload = json.dumps(
            ledger.to_dict(), ensure_ascii=False, sort_keys=True, indent=2
        ) + "\n"
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", dir=str(self.root)
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return target

    def load(self, turn_id: str) -> ExecutionLedger:
        raw = json.loads(self._path(turn_id).read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise LedgerInvariantError("ledger root must be an object")
        return ExecutionLedger.from_dict(raw)

    def all_ledgers(self) -> Iterable[ExecutionLedger]:
        if not self.root.exists():
            return ()
        ledgers: list[ExecutionLedger] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, Mapping):
                    ledgers.append(ExecutionLedger.from_dict(raw))
            except (OSError, json.JSONDecodeError, LedgerInvariantError):
                continue
        return tuple(ledgers)

    def reconcile_interrupted(self) -> tuple[ExecutionLedger, ...]:
        reconciled: list[ExecutionLedger] = []
        for ledger in self.all_ledgers():
            if ledger.is_terminal:
                continue
            ledger.transition(
                LifecycleState.ERROR,
                terminal_reason="unexpected_process_interruption",
            )
            self.save(ledger)
            reconciled.append(ledger)
        return tuple(reconciled)
