"""Remote-owned, owner-scoped read contract for durable cold restart receipts.

Functions can project these records, but only Remote admits and records outcomes.
No request, credentials, raw exceptions, or machine paths are returned to the UI.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import re
import time
import os
from contextlib import contextmanager

RESTART_ID_RE = re.compile(r"^rst_[0-9]{8}T[0-9]{6}Z_[a-f0-9]{12}$")

def verified_restart_receipt(
    payload: dict[str, Any], *, expected_target: str
) -> tuple[bool, str]:
    if payload.get("ok") is not True or payload.get("state") != "completed":
        return False, str(
            payload.get("error") or "restart did not reach a completed terminal state"
        )
    target = str(payload.get("target_instance") or "").strip().upper()
    if target != str(expected_target or "").strip().upper():
        return False, "restart terminal receipt target does not match the request"
    evidence = payload.get("evidence") or {}
    required = (
        "old_pid_exited",
        "new_pid_alive",
        "new_pid_differs",
        "backend_health_ok",
        "instance_matches",
        "runtime_version_verified",
        "generation_verified",
    )
    missing = [key for key in required if evidence.get(key) is not True]
    old_pid = evidence.get("old_pid")
    new_pid = evidence.get("new_pid")
    if missing or type(old_pid) is not int or type(new_pid) is not int or old_pid <= 0 or new_pid <= 0:
        detail = ", ".join(missing) or "PID evidence"
        return False, f"restart terminal evidence is incomplete: {detail}"
    if old_pid == new_pid:
        return False, "restart terminal evidence reused the old PID"
    if not isinstance(evidence.get("runtime_version"), dict):
        return False, "restart terminal evidence is missing the runtime version"
    if not str(evidence.get("generation_id") or "").strip():
        return False, "restart terminal evidence is missing the Function generation"
    if str(evidence.get("actual_instance") or "").strip().upper() != target:
        return False, "restart terminal evidence contains the wrong instance identity"
    return True, "ok"


@contextmanager
def open_restart_receipt(path):
    """A bounded reader closes its snapshot promptly; Windows writers retry briefly."""
    with Path(path).open("rb") as stream:
        yield stream


def read_restart_receipt_text(path, *, limit=1048576):
    with open_restart_receipt(path) as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("restart receipt exceeds read limit")
    return raw.decode("utf-8")


def replace_restart_receipt(temporary, path):
    # Brief external readers/antivirus may still deny delete sharing. Never
    # publish a partial file, silently drop progress, or retry indefinitely.
    deadline = time.monotonic() + 0.25
    while True:
        try:
            os.replace(temporary, path)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in {5, 32, 33} or time.monotonic() >= deadline:
                raise
            time.sleep(0.005)


def _epoch(value):
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None

def project_restart(record: dict, *, instance_id: str, owner_id: str, agent_id: str | None = None) -> dict | None:
    origin = record.get("notify_origin") or {}
    agent = record.get("requester_agent")
    instance = str(instance_id).upper()
    if (not isinstance(origin, dict) or origin.get("owner_id") != owner_id
        or not agent or origin.get("agent_id") != agent
        or (agent_id is not None and agent != agent_id)
        or str(origin.get("instance_id") or "").upper() != instance
        or str(record.get("target_instance") or "").upper() != instance
        or str(record.get("controlled_instance") or "").upper() != instance
        or not RESTART_ID_RE.fullmatch(str(record.get("restart_id") or ""))):
        return None
    state = record.get("state")
    terminal = state in {"completed", "failed", "rejected"}
    succeeded = state == "completed" and verified_restart_receipt(record, expected_target=instance)[0]
    timing = record.get("lifecycle_timing") or {}
    evidence = record.get("evidence") or {}
    phase = str(record.get("phase") or "accepted")[:80]
    sequence = 1 + len(timing.get("stages") or []) + int(terminal)
    from orchestrator.reboot_ui import restart_notice

    notice_status = (
        "succeeded" if succeeded else "unconfirmed"
        if terminal and (state == "completed" or phase == "terminal_verification")
        else "failed" if terminal else "running"
    )
    return {
        "notice": restart_notice(record, status=notice_status),
        "action": "restart", "kind": "cold", "mode": "cold",
        "operation_id": record["restart_id"], "source_agent": agent,
        "instance_id": instance, "accepted": state != "rejected",
        "status": "succeeded" if succeeded else "failed" if terminal else "running",
        "terminal": terminal, "outcome": "succeeded" if succeeded else "failed" if terminal else "unknown",
        "phase": phase, "reason": ("restart_unconfirmed" if terminal and not succeeded
            and (state == "completed" or phase == "terminal_verification") else None),
        "created_at": _epoch(record.get("started_at")), "finished_at": _epoch(record.get("completed_at")),
        "duration_ms": timing.get("total_ms"), "latest_sequence": sequence,
        "kernel_pid": evidence.get("new_pid") if succeeded else None,
        "shared_generation_id": evidence.get("generation_id") if succeeded else None,
        "progress": [{"sequence": sequence, "phase": phase}],
        "warnings": ["restart_notification_failed"] if (record.get("notification") or {}).get("state") == "failed" else [],
    }


class RestartReceiptReader:
    """Bounded, read-only access to Remote receipts through its public contract."""
    def __init__(self, root, *, instance_id: str):
        self.directory = Path(root) / "state" / "restarts"
        self.instance_id = instance_id

    def operation(self, operation_id: str, *, owner_id: str, agent_id: str | None = None):
        if not RESTART_ID_RE.fullmatch(operation_id):
            raise ValueError("invalid restart operation ID")
        path = self.directory / (operation_id + ".json")
        try:
            if path.is_symlink() or self.directory.is_symlink() or path.stat().st_size > 1048576:
                return None
            record = json.loads(read_restart_receipt_text(path), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            if not isinstance(record, dict) or record.get("restart_id") != operation_id:
                return None
            return project_restart(record, instance_id=self.instance_id, owner_id=owner_id, agent_id=agent_id)
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def visible_operations(self, *, owner_id: str, recent_seconds: float = 60):
        if not self.directory.exists() or self.directory.is_symlink():
            return []
        # Timestamp prefix makes names ordered; scan bounded recent receipts.
        paths = sorted(self.directory.glob("rst_*.json"), reverse=True)[:64]
        result = []
        for path in paths:
            operation = self.operation(path.stem, owner_id=owner_id)
            if operation is None:
                continue
            if operation["terminal"]:
                try:
                    finished = float(operation["finished_at"])
                    if time.time() - finished > recent_seconds:
                        continue
                except (ValueError, TypeError):
                    continue
            result.append(operation)
        return sorted(result, key=lambda row: str(row.get("created_at") or ""), reverse=True)
# HASHI-Fingerprint: hfp_4c8b7aaa6fb94fd5a3fb975b275f4775
