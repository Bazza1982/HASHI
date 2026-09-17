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
_READ_ONLY_PROFILES = frozenset({"query", "poll", "verify"})
_NO_PROCESS_JOB_STATES = frozenset({"created", "starting", "policy_denied", "start_failed"})


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


def _read_recent_jsonl(path: Path) -> tuple[list[dict[str, Any]], bool, str]:
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
        return [], False, "missing"
    except OSError:
        return [], False, "unreadable"
    rows: list[dict[str, Any]] = []
    for raw_line in content.splitlines():
        try:
            row = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(row, Mapping):
            rows.append(dict(row))
    return rows, bool(offset), "available"


def _tool_profile(tool_name: str) -> str:
    """Resolve the shared Smart Tool profile without copying its catalogue."""

    try:
        from tools.smart_tools import smart_tool_spec

        return str(smart_tool_spec(tool_name).profile or "unknown")
    except Exception:
        return "unknown"


def _tool_action(row: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    if source == "smart_tool_ledger":
        tool_name = str(row.get("tool") or "")
        return {
            "source": source,
            "request_id": str(row.get("request_id") or row.get("task_id") or ""),
            "recorded_at": row.get("timestamp"),
            "tool_name": tool_name,
            "tool_call_id": str(row.get("call_id") or ""),
            "status": str(row.get("status") or "unknown"),
            "effect": str(row.get("effect") or "unknown"),
            "profile": _tool_profile(tool_name),
            "target": str(row.get("target") or ""),
        }
    arguments = row.get("args_redacted")
    args = dict(arguments) if isinstance(arguments, Mapping) else {}
    details = row.get("details")
    detail_map = dict(details) if isinstance(details, Mapping) else {}
    tool_name = str(row.get("tool_name") or "")
    return {
        "source": source,
        "request_id": str(row.get("request_id") or ""),
        "recorded_at": row.get("ts"),
        "tool_name": tool_name,
        "tool_call_id": str(row.get("tool_call_id") or ""),
        "status": str(row.get("status") or "unknown"),
        "effect": str(detail_map.get("smart_effect") or "unknown"),
        "profile": _tool_profile(tool_name),
        "target": str(args.get("path") or ""),
    }


def _deduplicate_actions(actions: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Prefer the typed ledger when both ledgers describe the same Tool call."""

    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []
    for value in actions:
        row = dict(value)
        call_id = str(row.get("tool_call_id") or "")
        tool_name = str(row.get("tool_name") or "")
        if not call_id:
            unkeyed.append(row)
            continue
        key = (call_id, tool_name)
        existing = indexed.get(key)
        if existing is None or row.get("source") == "smart_tool_ledger":
            indexed[key] = row
    return [*indexed.values(), *unkeyed][-_MAX_ACTIONS:]


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


def _reconcile_request_evidence(
    *,
    terminal_projection: Mapping[str, Any],
    actions: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
    tool_logs_truncated: bool,
) -> dict[str, Any]:
    """Derive current completion/effect facts without changing execution policy."""

    terminal = terminal_projection.get("terminal")
    terminal_map = dict(terminal) if isinstance(terminal, Mapping) else {}
    completed_value = terminal_map.get("completed")
    if completed_value is True:
        completion = "completed"
    elif completed_value is False:
        completion = "not_completed"
    else:
        completion = "unknown"

    effect_record = terminal_projection.get("effects")
    effects_map = dict(effect_record) if isinstance(effect_record, Mapping) else {}
    declared_tool_count = max(0, int(effects_map.get("tool_call_count") or 0))
    known_effects: list[dict[str, Any]] = []
    unknown_reasons: list[str] = []
    read_only_actions: list[dict[str, str]] = []

    for action in actions:
        tool_name = str(action.get("tool_name") or "")
        status = str(action.get("status") or "unknown").casefold()
        effect = str(action.get("effect") or "unknown").casefold()
        profile = str(action.get("profile") or "unknown").casefold()
        call_id = str(action.get("tool_call_id") or "")
        if status == "success" and profile in _READ_ONLY_PROFILES and effect in {
            "observed",
            "no_change",
        }:
            read_only_actions.append({"tool_name": tool_name, "tool_call_id": call_id})
            continue
        if effect == "no_change":
            continue
        if effect == "changed" or (
            status == "success" and tool_name in _FILE_WRITE_TOOLS
        ):
            known_effects.append(
                {
                    "kind": "tool_change",
                    "tool_name": tool_name,
                    "tool_call_id": call_id,
                    "target": str(action.get("target") or ""),
                }
            )
            continue
        if status in {"success", "partial", "failed", "unknown"}:
            unknown_reasons.append(f"tool_effect_unknown:{tool_name or 'unnamed'}")

    for job in jobs:
        job_id = str(job.get("job_id") or "")
        state = str(job.get("state") or "unknown").casefold()
        if state == "succeeded":
            known_effects.append(
                {
                    "kind": "background_job_completed",
                    "job_id": job_id,
                    "state": state,
                    "returncode": job.get("returncode"),
                }
            )
        elif state not in _NO_PROCESS_JOB_STATES:
            unknown_reasons.append(f"background_job_effect_unknown:{job_id or 'unnamed'}:{state}")

    if declared_tool_count > len(actions):
        unknown_reasons.append("tool_receipts_incomplete")
    if tool_logs_truncated and declared_tool_count:
        unknown_reasons.append("tool_receipts_truncated")
    if any(
        action.get("tool_name") == "background_job_start"
        and str(action.get("status") or "").casefold() == "success"
        for action in actions
    ) and not jobs:
        unknown_reasons.append("background_job_receipt_missing")
    coarse_side_effects = effects_map.get("side_effects_possible")
    receipts_prove_read_only = bool(
        declared_tool_count
        and len(actions) >= declared_tool_count
        and len(read_only_actions) == len(actions)
        and not known_effects
        and not unknown_reasons
    )
    if coarse_side_effects is True and not known_effects and not receipts_prove_read_only:
        unknown_reasons.append("terminal_side_effects_possible")
    elif coarse_side_effects is None and not known_effects:
        unknown_reasons.append("terminal_side_effect_evidence_missing")

    unknown_reasons = list(dict.fromkeys(unknown_reasons))
    if known_effects:
        effect_status = "observed"
    elif unknown_reasons:
        effect_status = "unknown"
    elif terminal_projection and declared_tool_count <= len(actions):
        effect_status = "none_observed"
    else:
        effect_status = "unknown"

    retryable = terminal_map.get("error_retryable")
    if completion == "completed":
        safe_retry = {"status": "not_applicable", "reasons": ["request_completed"]}
    elif known_effects:
        safe_retry = {"status": "absent", "reasons": ["completed_effects_observed"]}
    elif effect_status == "unknown" or completion == "unknown":
        safe_retry = {
            "status": "unknown",
            "reasons": unknown_reasons or ["insufficient_evidence"],
        }
    elif retryable is True:
        safe_retry = {
            "status": "present",
            "reasons": ["provider_marked_retryable", "no_effect_evidence"],
        }
    elif retryable is False:
        safe_retry = {"status": "absent", "reasons": ["provider_marked_nonretryable"]}
    else:
        safe_retry = {"status": "unknown", "reasons": ["retryability_unknown"]}

    return {
        "request_completion": completion,
        "terminal_state": str(terminal_map.get("state") or "unknown"),
        "effect_status": effect_status,
        "known_effects": known_effects,
        "unknown_effect_reasons": unknown_reasons,
        "read_only_actions": read_only_actions,
        "tool_receipts_observed": len(actions),
        "tool_receipts_declared": declared_tool_count,
        "safe_retry_evidence": safe_retry,
    }


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

    tool_rows, tool_truncated, tool_log_status = _read_recent_jsonl(
        workspace / "tool_action_audit.jsonl"
    )
    smart_rows, smart_truncated, smart_log_status = _read_recent_jsonl(
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
    actions = _deduplicate_actions(actions)
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

    reconciliation = _reconcile_request_evidence(
        terminal_projection=terminal_projection,
        actions=actions,
        jobs=jobs,
        tool_logs_truncated=tool_truncated or smart_truncated,
    )

    terminal = terminal_projection.get("terminal")
    provider = terminal_projection.get("provider")
    return {
        "format": FORMAT,
        "request_id": request,
        "terminal": (
            dict(terminal)
            if isinstance(terminal, Mapping)
            else {"state": "unknown", "completed": None}
        ),
        "provider": dict(provider) if isinstance(provider, Mapping) else {},
        "reconciliation": reconciliation,
        "safe_retry_evidence": reconciliation["safe_retry_evidence"],
        "file_writes": file_writes,
        "tool_actions": actions,
        "background_jobs": jobs,
        "evidence": {
            "terminal_projection": str(projection_path(workspace, request)),
            "tool_action_audit": str(workspace / "tool_action_audit.jsonl"),
            "smart_tool_ledger": str(workspace / "tool_ledger.jsonl"),
            "tool_log_suffix_truncated": tool_truncated,
            "smart_log_suffix_truncated": smart_truncated,
            "tool_action_audit_status": tool_log_status,
            "smart_tool_ledger_status": smart_log_status,
        },
    }


__all__ = [
    "FORMAT",
    "build_request_diagnostics",
    "projection_path",
    "safe_request_id",
]
