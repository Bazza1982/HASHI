"""Read-only request diagnostics assembled from existing durable evidence.

This module is deliberately a projection, not another execution state machine.
Terminal state is written by ``runtime_debug_reporting``; tool and background
evidence remain owned by their existing ledgers and receipts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections import deque
from pathlib import Path
from typing import Any, Iterable, Mapping


FORMAT = "hashi-request-diagnostics-v1"
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_LOG_BYTES = 16 * 1024 * 1024
_MAX_DIAGNOSTIC_SCAN_BYTES = 128 * 1024 * 1024
_MAX_DIAGNOSTIC_RECORD_BYTES = 4 * 1024 * 1024
_MAX_ACTIONS = 256
_FILE_WRITE_TOOLS = frozenset({"file_write", "apply_patch"})


def _confirmed_read(action: Mapping[str, Any]) -> bool:
    from tools.effect_receipts import is_verified_read_effect_receipt

    receipt = action.get("effect_receipt")
    return is_verified_read_effect_receipt(
        tool_name=str(action.get("tool_name") or ""),
        tool_call_id=str(action.get("tool_call_id") or ""),
        receipt=receipt if isinstance(receipt, Mapping) else None,
        completed=action.get("status") == "success",
        status=action.get("status"),
    )


def safe_request_id(request_id: str) -> str:
    value = _SAFE_ID.sub("_", str(request_id or "")).strip("._")
    if not value:
        raise ValueError("request_id is required")
    return value[:180]


def projection_path(workspace_dir: Path, request_id: str) -> Path:
    return (
        Path(workspace_dir)
        / "backend_state"
        / "request_diagnostics"
        / f"{safe_request_id(request_id)}.json"
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _read_recent_jsonl(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """Read a bounded suffix and state explicitly when older rows were omitted."""

    try:
        size = path.stat().st_size
        offset = max(0, size - _MAX_LOG_BYTES)
        with path.open("rb") as handle:
            handle.seek(offset)
            if offset:
                handle.readline()  # discard a partial record
            content = handle.read(_MAX_LOG_BYTES)
    except FileNotFoundError:
        return [], False  # Individual ledgers are optional; coverage is checked below.
    except OSError:
        return [], True
    rows: list[dict[str, Any]] = []
    incomplete = bool(offset)
    for raw_line in content.splitlines():
        try:
            row = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            incomplete = True
            continue
        if isinstance(row, Mapping):
            rows.append(dict(row))
        else:
            incomplete = True
    return rows, incomplete


def _read_matching_jsonl(path: Path, request_id: str) -> tuple[list[dict[str, Any]], bool]:
    """Search a bounded snapshot for one request without loading the whole ledger."""
    matches: deque[dict[str, Any]] = deque(maxlen=_MAX_ACTIONS + 1)
    incomplete = False
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            end = before.st_size
            offset = max(0, end - _MAX_DIAGNOSTIC_SCAN_BYTES)
            handle.seek(offset)

            def discard_record() -> None:
                while handle.tell() < end:
                    part = handle.readline(min(_MAX_DIAGNOSTIC_RECORD_BYTES, end - handle.tell()))
                    if not part or part.endswith(b"\n"):
                        break

            if offset:
                incomplete = True
                discard_record()
            while handle.tell() < end:
                remaining = end - handle.tell()
                raw_line = handle.readline(min(_MAX_DIAGNOSTIC_RECORD_BYTES + 1, remaining))
                if not raw_line:
                    incomplete = True
                    break
                if len(raw_line) > _MAX_DIAGNOSTIC_RECORD_BYTES or not raw_line.endswith(b"\n"):
                    incomplete = True
                    if raw_line and not raw_line.endswith(b"\n"):
                        discard_record()
                    continue
                try:
                    row = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    incomplete = True
                    continue
                if not isinstance(row, Mapping):
                    incomplete = True
                elif str(row.get("request_id") or row.get("task_id") or "") == request_id:
                    matches.append(dict(row))
            after = os.fstat(handle.fileno())
            if after.st_size != end or after.st_mtime_ns != before.st_mtime_ns:
                incomplete = True
    except FileNotFoundError:
        return [], False
    except OSError:
        return list(matches), True
    return list(matches), incomplete


def _tool_action(row: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    if source == "smart_tool_ledger":
        return {
            "source": source,
            "request_id": str(row.get("request_id") or row.get("task_id") or ""),
            "recorded_at": row.get("timestamp"),
            "tool_name": str(row.get("tool") or ""),
            "tool_call_id": str(row.get("call_id") or ""),
            "status": str(row.get("status") or "unknown"),
            "effect": str(row.get("effect") or "unknown"),
            "target": str(row.get("target") or ""),
            "effect_receipt": dict(row["effect_receipt"]) if isinstance(row.get("effect_receipt"), Mapping) else None,
        }
    arguments = row.get("args_redacted")
    args = dict(arguments) if isinstance(arguments, Mapping) else {}
    details = row.get("details")
    detail_map = dict(details) if isinstance(details, Mapping) else {}
    return {
        "source": source,
        "request_id": str(row.get("request_id") or ""),
        "recorded_at": row.get("ts"),
        "tool_name": str(row.get("tool_name") or ""),
        "tool_call_id": str(row.get("tool_call_id") or ""),
        "status": str(row.get("status") or "unknown"),
        "effect": str(detail_map.get("smart_effect") or "unknown"),
        "target": str(args.get("path") or ""),
        "effect_receipt": dict(detail_map["effect_receipt"]) if isinstance(detail_map.get("effect_receipt"), Mapping) else None,
    }


def read_scoped_recovery_evidence(
    *, agent_home: Path, request_id: str, evidence_ref: str,
    item_id: str, offset: int, expected_digest: str, scope: Mapping[str, Any],
) -> dict[str, Any]:
    """Read one recovery value only within the caller's authenticated Session."""
    unavailable = {"status": "not available"}
    raw_ref = str(evidence_ref or "")
    if raw_ref.startswith("her_provider_context_items:"):
        parts = raw_ref.split(":", 2)
        if len(parts) != 3:
            return unavailable
        field, session_id, turn_id = "provider_tool_result", parts[1], parts[2]
    else:
        parts = raw_ref.split(":", 3)
        if (
            len(parts) != 4
            or parts[0] not in {"her_active_turn_recovery", "her_settled_checkpoints"}
            or parts[1] not in {"tool_receipts", "side_effects"}
        ):
            return unavailable
        field, session_id, turn_id = parts[1], parts[2], parts[3]
    if not session_id or not turn_id or type(offset) is not int or offset < 0:
        return unavailable
    allowed_scopes = scope.get("_herv3_recovery_scopes")
    if not isinstance(allowed_scopes, (list, tuple)) or not any(
        isinstance(item, Mapping)
        and item.get("session_id") == session_id
        and item.get("turn_id") == turn_id
        and item.get("request_id") == request_id
        for item in allowed_scopes
    ):
        return unavailable
    agent = str(scope.get("agent_name") or "")
    owner = str(scope.get("owner_id") or "")
    conversation = str(scope.get("hashi_session_id") or "")
    instance = str(scope.get("instance_id") or getattr(scope.get("global_config"), "instance_id", "") or "")
    generation = scope.get("context_generation")
    workzone = str(scope.get("session_workspace") or agent_home)
    if not agent or not owner or not conversation or not instance or type(generation) is not int:
        return unavailable
    from orchestrator.her_v2.session_store import _binding_workzone_identity

    workzone = _binding_workzone_identity(
        workzone, hashi_conversation_id=conversation, context_generation=generation,
    )
    db_path = Path(agent_home) / "backend_state" / "her_v2" / "fixed_sessions.sqlite3"
    if not db_path.is_file():
        return unavailable
    try:
        with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT r.tool_receipts_json, r.side_effects_json, r.provider_context_json,
                       r.provider_context_digest, r.provider_context_producer_claim_json,
                       r.provider_context_producer_claim_digest,
                       s.workzone_identity AS stored_workzone
                FROM her_active_turn_recovery r
                JOIN her_turns t ON t.session_id = r.session_id AND t.turn_id = r.turn_id
                JOIN her_sessions s ON s.session_id = r.session_id
                WHERE r.session_id = ? AND r.turn_id = ? AND t.request_id = ?
                  AND s.agent_id = ? COLLATE NOCASE AND s.owner_id = ?
                  AND s.instance_id = ? COLLATE NOCASE
                  AND s.hashi_conversation_id = ? AND s.context_generation = ?
                """,
                (session_id, turn_id, str(request_id), agent, owner, instance,
                 conversation, generation),
            ).fetchone()
            if row is None or _binding_workzone_identity(
                row["stored_workzone"], hashi_conversation_id=conversation, context_generation=generation,
            ) != workzone:
                return unavailable
            if field == "provider_tool_result":
                from orchestrator.her_v2.session_store import (
                    HerSessionStore, _PROVIDER_CONTEXT_STORAGE, _digest,
                )

                claim = HerSessionStore._normalise_run_claim(
                    json.loads(row["provider_context_producer_claim_json"]),
                    expected_request_id=request_id,
                )
                if (
                    not claim or claim.get("state") != "running"
                    or _digest(claim) != row["provider_context_producer_claim_digest"]
                ):
                    return unavailable
                metadata = json.loads(row["provider_context_json"])
                context_digest = str(row["provider_context_digest"] or "")
                if not isinstance(metadata, Mapping):
                    return unavailable
                if metadata.get("storage") == _PROVIDER_CONTEXT_STORAGE:
                    context = HerSessionStore._read_segmented_provider_context(
                        connection, session_id=session_id, turn_id=turn_id,
                        metadata=metadata, expected_digest=context_digest,
                    )
                elif context_digest and _digest(metadata) == context_digest:
                    context = metadata
                else:
                    return unavailable
                if not item_id:
                    # Scoped discovery: the caller holds the canonical
                    # reference but not an item identity. Return only the
                    # authorized provider-item identities in this checkpoint.
                    item_ids = sorted({
                        str(message.get("tool_call_id") or "")
                        for message in context.get("messages", [])
                        if isinstance(message, Mapping) and message.get("role") == "tool"
                        and str(message.get("tool_call_id") or "")
                    })
                    value = {"field": field, "item_ids": item_ids, "count": len(item_ids)}
                else:
                    matches = [
                        message for message in context.get("messages", [])
                        if isinstance(message, Mapping) and message.get("role") == "tool"
                        and str(message.get("tool_call_id") or "") == item_id
                    ]
                    if len(matches) != 1:
                        return unavailable
                    value = {"tool_call_id": item_id, "content": matches[0].get("content")}
            else:
                value = json.loads(row["tool_receipts_json"] if field == "tool_receipts" else row["side_effects_json"])
                if not isinstance(value, list):
                    return unavailable
                if item_id:
                    matches = [
                        item for item in value if isinstance(item, Mapping)
                        and any(str(item.get(key) or "") == item_id
                                for key in ("operation_id", "tool_call_id", "invocation_id", "evidence_ref"))
                    ]
                    if len(matches) != 1:
                        return unavailable
                    value = matches[0]
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        if (offset and not expected_digest) or (expected_digest and expected_digest != digest):
            return unavailable
        content = serialized[offset:offset + 2048]
        next_offset = offset + len(content)
        return {
            "status": "available", "field": field, "digest": digest,
            "content": content,
            "next_offset": next_offset if next_offset < len(serialized) else None,
        }
    except (OSError, sqlite3.Error, ValueError, TypeError, RuntimeError):
        return unavailable


def _job_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        converted = converter()
        return dict(converted) if isinstance(converted, Mapping) else {}
    return {}


def _matching_background_jobs(
    request_id: str,
    jobs: Iterable[Any],
    history: Mapping[str, Iterable[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for value in jobs:
        row = _job_dict(value)
        origin = row.get("origin")
        if not isinstance(origin, Mapping):
            continue
        if str(origin.get("request_id") or "") != request_id:
            continue
        job_id = str(row.get("job_id") or "")
        matched.append(
            {
                "job_id": job_id,
                "state": str(row.get("state") or "unknown"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "ended_at": row.get("ended_at"),
                "returncode": row.get("returncode"),
                "error": row.get("error"),
                "history": [dict(item) for item in history.get(job_id, ())],
            }
        )
    return matched


def build_request_diagnostics(
    *,
    workspace_dir: Path,
    request_id: str,
    background_jobs: Iterable[Any] = (),
    background_history: Mapping[str, Iterable[Mapping[str, Any]]] | None = None,
    _include_reconciliation: bool = True,
) -> dict[str, Any]:
    """Join sanitised terminal, tool, and background evidence by request id."""

    request = str(request_id or "").strip()
    safe_request_id(request)
    workspace = Path(workspace_dir)
    terminal_projection = _read_json(projection_path(workspace, request))

    if _include_reconciliation:
        tool_rows, tool_truncated = _read_matching_jsonl(workspace / "tool_action_audit.jsonl", request)
        smart_rows, smart_truncated = _read_matching_jsonl(workspace / "tool_ledger.jsonl", request)
    else:
        # Automatic terminal reconciliation remains bounded to the recent suffix.
        tool_rows, tool_truncated = _read_recent_jsonl(workspace / "tool_action_audit.jsonl")
        smart_rows, smart_truncated = _read_recent_jsonl(workspace / "tool_ledger.jsonl")
    actions = [
        _tool_action(row, source="tool_action_audit")
        for row in tool_rows
        if str(row.get("request_id") or "") == request
    ]
    actions.extend(
        _tool_action(row, source="smart_tool_ledger")
        for row in smart_rows
        if str(row.get("request_id") or row.get("task_id") or "") == request
    )
    actions_truncated = len(actions) > _MAX_ACTIONS
    actions = actions[-_MAX_ACTIONS:]
    file_writes = [
        {
            "tool_name": row["tool_name"],
            "tool_call_id": row["tool_call_id"],
            "target": row["target"],
            "status": row["status"],
            "effect": row["effect"],
        }
        for row in actions
        if row["tool_name"] in _FILE_WRITE_TOOLS and row["target"]
    ]
    jobs = _matching_background_jobs(
        request,
        background_jobs,
        background_history or {},
    )

    terminal = terminal_projection.get("terminal")
    provider = terminal_projection.get("provider")
    effects = terminal_projection.get("effects")
    retry = terminal_projection.get("safe_retry_evidence")
    report = {
        "format": FORMAT,
        "request_id": request,
        "terminal": (
            dict(terminal)
            if isinstance(terminal, Mapping)
            else {"state": "unknown", "completed": None}
        ),
        "provider": dict(provider) if isinstance(provider, Mapping) else {},
        "effects": dict(effects) if isinstance(effects, Mapping) else {},
        "safe_retry_evidence": (
            dict(retry)
            if isinstance(retry, Mapping)
            else {"status": "unknown", "reasons": ["terminal_projection_missing"]}
        ),
        "file_writes": file_writes,
        "tool_actions": actions,
        "background_jobs": jobs,
        "evidence": {
            "terminal_projection": str(projection_path(workspace, request)),
            "tool_action_audit": str(workspace / "tool_action_audit.jsonl"),
            "smart_tool_ledger": str(workspace / "tool_ledger.jsonl"),
            "tool_log_suffix_truncated": tool_truncated,
            "smart_log_suffix_truncated": smart_truncated,
            "actions_truncated": actions_truncated,
            "tool_log_missing": not (workspace / "tool_action_audit.jsonl").is_file(),
            "smart_log_missing": not (workspace / "tool_ledger.jsonl").is_file(),
        },
    }
    if _include_reconciliation:
        terminal_map = report["terminal"]
        effects_map = report["effects"]
        try:
            tool_count = max(0, int(effects_map.get("tool_call_count") or 0))
        except (TypeError, ValueError):
            tool_count = 0
        side_effects = effects_map.get("side_effects_possible")
        summary = build_user_effect_reconciliation(
            workspace_dir=workspace,
            request_id=request,
            tool_call_count=tool_count,
            side_effects_possible=side_effects is not False,
            _prefetched_reports={workspace: report},
        )
        # The saved terminal summary can include other authorized workspaces.
        # A later, narrower query must not erase evidence it already confirmed.
        saved = effects_map.get("reconciliation")
        if isinstance(saved, Mapping):
            for key in (
                "confirmed_read_count", "confirmed_write_count", "observed_tool_count",
                "unverified_action_count", "completed_background_job_count",
                "no_change_count", "completed_action_count", "pending_action_count",
            ):
                value = saved.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    summary[key] = max(summary[key], value)
            if saved.get("evidence_limited") is True:
                summary["evidence_limited"] = True
        known_effects = []
        seen_writes: set[str] = set()
        for action in actions:
            receipt = action.get("effect_receipt")
            call_id = str(action.get("tool_call_id") or "")
            if (
                action.get("tool_name") in _FILE_WRITE_TOOLS
                and action.get("status") == "success"
                and isinstance(receipt, Mapping)
                and receipt.get("kind") == "write"
                and receipt.get("readback") is True
                and call_id not in seen_writes
            ):
                seen_writes.add(call_id)
                known_effects.append({
                    "kind": "tool_change",
                    "tool_name": action["tool_name"],
                    "tool_call_id": call_id,
                    "target": action["target"],
                })
        for job in jobs:
            if job["state"] == "succeeded":
                known_effects.append({
                    "kind": "background_job_completed",
                    "job_id": job["job_id"],
                    "state": job["state"],
                    "returncode": job["returncode"],
                })
        if summary["confirmed_write_count"] > len(seen_writes):
            known_effects.append({
                "kind": "terminal_confirmed_write",
                "count": summary["confirmed_write_count"] - len(seen_writes),
            })
        completed_jobs = sum(job["state"] == "succeeded" for job in jobs)
        if summary["completed_background_job_count"] > completed_jobs:
            known_effects.append({
                "kind": "terminal_completed_background_job",
                "count": summary["completed_background_job_count"] - completed_jobs,
            })
        unknown_reasons = []
        if summary["evidence_limited"]:
            unknown_reasons.append("tool_evidence_incomplete")
        if summary["unverified_action_count"] or summary["pending_action_count"]:
            unknown_reasons.append("tool_effect_unverified")
        if any(job["state"] != "succeeded" for job in jobs):
            unknown_reasons.append("background_job_effect_unverified")
        completed = terminal_map.get("completed")
        completion = "completed" if completed is True else (
            "not_completed" if completed is False else "unknown"
        )
        if completion == "unknown":
            unknown_reasons.append("terminal_projection_missing")
        effect_status = (
            "observed" if known_effects else
            "unknown" if unknown_reasons else
            "none_observed"
        )
        report["reconciliation"] = {
            **summary,
            "request_completion": completion,
            "effect_status": effect_status,
            "known_effects": known_effects,
            "unknown_effect_reasons": unknown_reasons,
        }
        # A later receipt can invalidate a previously saved retry indication.
        # Never upgrade an existing conservative retry result at query time.
        if completed is not True and known_effects:
            report["safe_retry_evidence"] = {
                "status": "absent", "reasons": ["completed_effects_observed"]
            }
        elif unknown_reasons and report["safe_retry_evidence"].get("status") == "present":
            report["safe_retry_evidence"] = {
                "status": "unknown", "reasons": ["effect_audit_incomplete"]
            }
    return report


def build_user_effect_reconciliation(
    *,
    workspace_dir: Path,
    request_id: str,
    tool_call_count: int,
    side_effects_possible: bool,
    additional_workspaces: Iterable[Path] = (),
    background_jobs: Iterable[Any] = (),
    _prefetched_reports: Mapping[Path, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Summarize observed effects without inferring success from a tool exit.

    A write is confirmed only when a readback receipt exists. Other actions
    remain unverified, including a CLI failure with no logged tool call.
    """

    locations = dict.fromkeys(
        [Path(workspace_dir), *(Path(path) for path in additional_workspaces)]
    )
    by_call: dict[str, dict[str, Any]] = {}
    evidence_limited = False
    jobs_by_id: dict[str, dict[str, Any]] = {}
    for location in locations:
        report = (_prefetched_reports or {}).get(location)
        if report is None:
            report = build_request_diagnostics(
                workspace_dir=location,
                request_id=request_id,
                background_jobs=background_jobs,
                _include_reconciliation=False,
            )
        evidence = report["evidence"]
        evidence_limited = evidence_limited or bool(
            evidence["tool_log_suffix_truncated"]
            or evidence["smart_log_suffix_truncated"]
            or evidence["actions_truncated"]
        )
        for index, action in enumerate(report["tool_actions"]):
            call_id = str(action.get("tool_call_id") or "")
            key = call_id or f"{location}:{action.get('source')}:{index}"
            previous = by_call.get(key)
            terminal_statuses = {"success", "failed", "blocked"}
            previous_terminal = previous is not None and previous.get("status") in terminal_statuses
            current_terminal = action.get("status") in terminal_statuses
            if previous is None or (current_terminal and not previous_terminal) or (
                current_terminal == previous_terminal
                and not isinstance(previous.get("effect_receipt"), Mapping)
                and isinstance(action.get("effect_receipt"), Mapping)
            ):
                by_call[key] = action
            elif current_terminal and previous_terminal and action.get("effect") == "no_change" and previous.get("effect") == "unknown":
                by_call[key] = {**previous, "effect":"no_change"}
        for job in report["background_jobs"]:
            jobs_by_id[str(job.get("job_id") or "")] = job
    confirmed_reads = sum(1 for action in by_call.values() if _confirmed_read(action))
    confirmed_writes = sum(
        1
        for action in by_call.values()
        if action.get("tool_name") in _FILE_WRITE_TOOLS
        and action.get("status") == "success"
        and isinstance(action.get("effect_receipt"), Mapping)
        and action["effect_receipt"].get("kind") == "write"
        and action["effect_receipt"].get("readback") is True
    )
    observed = max(max(0, int(tool_call_count)), len(by_call))
    terminal_statuses = {"success", "failed", "blocked"}
    no_change = sum(
        1 for action in by_call.values()
        if action.get("effect") == "no_change" and action.get("status") in terminal_statuses
    )
    completed = sum(1 for action in by_call.values() if action.get("status") in terminal_statuses)
    pending = len(by_call) - completed
    evidence_limited = evidence_limited or len(by_call) < max(0, int(tool_call_count))
    if side_effects_possible and not by_call:
        evidence_limited = True
    unverified = max(0, observed - confirmed_reads - confirmed_writes - no_change)
    if (
        side_effects_possible
        and not confirmed_reads
        and not confirmed_writes
        and not unverified
    ):
        unverified = 1
    return {
        "confirmed_read_count": confirmed_reads,
        "confirmed_write_count": confirmed_writes,
        "no_change_count": no_change,
        "completed_action_count": completed,
        "pending_action_count": pending,
        "observed_tool_count": observed,
        "unverified_action_count": unverified,
        "completed_background_job_count": sum(
            1 for job in jobs_by_id.values() if job.get("state") == "succeeded"
        ),
        "evidence_limited": evidence_limited,
    }


__all__ = [
    "FORMAT",
    "build_request_diagnostics",
    "build_user_effect_reconciliation",
    "projection_path",
    "safe_request_id",
]
