"""Read-only request diagnostics assembled from existing durable evidence.

This module is deliberately a projection, not another execution state machine.
Terminal state is written by ``runtime_debug_reporting``; tool and background
evidence remain owned by their existing ledgers and receipts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping


FORMAT = "hashi-request-diagnostics-v1"
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_LOG_BYTES = 16 * 1024 * 1024
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
) -> dict[str, Any]:
    """Join sanitised terminal, tool, and background evidence by request id."""

    request = str(request_id or "").strip()
    safe_request_id(request)
    workspace = Path(workspace_dir)
    terminal_projection = _read_json(projection_path(workspace, request))

    tool_rows, tool_truncated = _read_recent_jsonl(
        workspace / "tool_action_audit.jsonl"
    )
    smart_rows, smart_truncated = _read_recent_jsonl(
        workspace / "tool_ledger.jsonl"
    )
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
    return {
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


def build_user_effect_reconciliation(
    *,
    workspace_dir: Path,
    request_id: str,
    tool_call_count: int,
    side_effects_possible: bool,
    additional_workspaces: Iterable[Path] = (),
    background_jobs: Iterable[Any] = (),
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
        report = build_request_diagnostics(
            workspace_dir=location,
            request_id=request_id,
            background_jobs=background_jobs,
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
