"""PAO Phone action projection, backed by the existing Session Store transaction."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
from typing import Any

from .protocol import CallBinding, LiveVoiceError, stable_digest


ACTION_STATUSES = frozenset({"accepted", "running", "verified", "failed", "unknown", "cancelled"})


class PhoneActions:
    def __init__(self, store: Any):
        self.store = store

    @staticmethod
    def public(row: Mapping[str, Any]) -> dict[str, Any]:
        refs = json.loads(row["evidence_json"] or "[]")
        return {
            "action_id": row["action_id"], "kind": row["kind"], "status": row["status"],
            "summary": row["request"], "target_action_id": row["target_action_id"],
            "evidence_refs": refs, "evidence_ref": refs[0] if refs else None,
            "spoken_receipt": row["receipt"], "updated_at": row["updated_at"],
        }

    def rows(self, binding: CallBinding, *, delegation_id: str | None = None) -> list[dict[str, Any]]:
        with self.store._lock, self.store._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM live_actions WHERE call_id = ? "
                + ("AND delegation_id = ? " if delegation_id else "")
                + "ORDER BY created_at DESC LIMIT 32",
                (binding.call_id, delegation_id) if delegation_id else (binding.call_id,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def snapshot(self, binding: CallBinding) -> list[dict[str, Any]]:
        return [self.public(row) for row in self.rows(binding)]

    def create(self, binding: CallBinding, delegation_id: str, intents: Any) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        with self.store._lock, self.store._connection() as connection:
            for index, intent in enumerate(intents):
                action_id = "action-" + stable_digest({"call": binding.call_id, "delegation": delegation_id, "index": index})[:28]
                connection.execute(
                    """INSERT OR IGNORE INTO live_actions(call_id, action_id, delegation_id,
                    kind, request, target_action_id, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'accepted', ?, ?)""",
                    (binding.call_id, action_id, delegation_id, intent.kind,
                     intent.request, intent.target_action_id, now, now),
                )
        return self.rows(binding, delegation_id=delegation_id)

    def transition(self, binding: CallBinding, action_id: str, status: str, *,
                   run_id: str | None = None, evidence_refs: list[str] | None = None,
                   receipt: str = "") -> dict[str, Any]:
        refs = list(evidence_refs or [])
        if status not in ACTION_STATUSES or status == "verified" and not refs:
            raise LiveVoiceError("live_action_receipt_invalid", 409)
        with self.store._lock, self.store._connection() as connection:
            row = connection.execute("SELECT * FROM live_actions WHERE call_id = ? AND action_id = ?",
                                     (binding.call_id, action_id)).fetchone()
            if row is None:
                raise LiveVoiceError("live_action_not_found", 404)
            # A terminal effect cannot silently re-enter execution on reconnect.
            if row["status"] in {"verified", "failed", "unknown", "cancelled"} and status in {"accepted", "running"}:
                return self.public(row)
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                "UPDATE live_actions SET status = ?, run_id = COALESCE(?, run_id), evidence_json = ?, receipt = ?, updated_at = ? WHERE call_id = ? AND action_id = ?",
                (status, run_id, json.dumps(refs), receipt, now, binding.call_id, action_id),
            )
            updated = dict(row)
            updated.update(status=status, evidence_json=json.dumps(refs), receipt=receipt, updated_at=now)
            public = self.public(updated)
            self.store._append_event(connection, session_id=binding.session_id, run_id=run_id or row["run_id"],
                kind="voice.live.action.state", status=status, summary=str(row["request"])[:160],
                detail={"schema": "hashi.live_voice.event.v1", "scope": binding.public_scope(), **public})
        return public


def effect_evidence(kind: str, diagnostics: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Admit only successful deterministic tool receipts, never model prose."""
    evidence = []
    for row in diagnostics.get("tool_actions") or []:
        if not isinstance(row, Mapping) or row.get("status") != "success":
            continue
        receipt = row.get("effect_receipt")
        if not isinstance(receipt, Mapping) or not receipt.get("evidence_ref"):
            continue
        if kind == "query" and receipt.get("kind") != "read":
            continue
        if kind in {"write", "modify"} and receipt.get("kind") != "write":
            continue
        if receipt.get("kind") == "write" and (receipt.get("readback") is not True or not receipt.get("revision")):
            continue
        if kind == "cancel":
            continue
        evidence.append(dict(receipt))
    return evidence
