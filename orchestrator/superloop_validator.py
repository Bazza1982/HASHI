from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.superloop_store import SuperloopStore


ALLOWED_LOOP_STATUSES = {"draft", "running", "waiting", "blocked", "paused", "completed", "aborted", "failed"}
ALLOWED_TASK_STATUSES = {"pending", "in_progress", "waiting", "blocked", "completed", "skipped", "failed"}
ALLOWED_WAIT_STATUSES = {
    "pending",
    "open",
    "satisfied",
    "resolved",
    "completed",
    "closed",
    "timeout",
    "timed_out",
    "cancelled",
    "stale",
}
ALLOWED_ISSUE_STATUSES = {"open", "in_progress", "resolved", "closed", "waived", "stale"}
REQUIRED_LOOP_FILES = ("state.json", "taskboard.json", "issues.json", "waits.json", "events.jsonl")
TRUTH_CLAIM_STATUSES = {"completed"}


@dataclass(frozen=True)
class SuperloopFinding:
    severity: str
    code: str
    message: str
    ref: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {"severity": self.severity, "code": self.code, "message": self.message}
        if self.ref:
            payload["ref"] = self.ref
        return payload


def validate_loop(store: SuperloopStore, loop_id: str, *, closeout: bool = False) -> dict[str, Any]:
    """Validate a loop without mutating it.

    Default mode is advisory. Closeout mode promotes truth-claim violations to
    blocking errors so agents can keep working but cannot silently declare a loop
    complete without evidence.
    """
    loop_dir = store.loop_dir(loop_id)
    findings: list[SuperloopFinding] = []

    if not loop_dir.exists():
        return {
            "ok": False,
            "loop_id": loop_id,
            "closeout": closeout,
            "summary": {"errors": 1, "warnings": 0, "info": 0, "legacy": 0},
            "findings": [
                SuperloopFinding("error", "loop_missing", f"Loop not found: {loop_id}", str(loop_dir)).as_dict()
            ],
            "blocking": True,
        }

    for filename in REQUIRED_LOOP_FILES:
        if not (loop_dir / filename).exists():
            severity = "error" if closeout and filename != "events.jsonl" else "warn"
            findings.append(
                SuperloopFinding(severity, "required_file_missing", f"Missing required loop file: {filename}", filename)
            )

    state = _load_json_object(loop_dir / "state.json", findings, "state.json")
    tasks = _load_json_list(_resolve_json_path(store, loop_id, state, "taskboard_path", "taskboard.json"), findings, "taskboard.json")
    issues = _load_json_list(_resolve_json_path(store, loop_id, state, "issues_path", "issues.json"), findings, "issues.json")
    waits = _load_json_list(_resolve_json_path(store, loop_id, state, "waits_path", "waits.json"), findings, "waits.json")
    events = _load_jsonl_list(loop_dir / "events.jsonl", findings, "events.jsonl")

    _validate_state(state, findings, closeout=closeout)
    _validate_tasks(store, loop_id, tasks, findings, closeout=closeout)
    _validate_issues(issues, findings, closeout=closeout)
    _validate_waits(waits, findings, closeout=closeout)
    _validate_events(events, findings)
    _validate_closeout_shape(state, tasks, issues, waits, findings, closeout=closeout)

    counts = {
        "errors": sum(1 for item in findings if item.severity == "error"),
        "warnings": sum(1 for item in findings if item.severity == "warn"),
        "info": sum(1 for item in findings if item.severity == "info"),
        "legacy": sum(1 for item in findings if item.severity == "legacy"),
    }
    return {
        "ok": counts["errors"] == 0,
        "loop_id": loop_id,
        "closeout": closeout,
        "summary": counts,
        "findings": [item.as_dict() for item in findings],
        "blocking": closeout and counts["errors"] > 0,
    }


def format_validation_report(report: dict[str, Any], *, max_findings: int = 12) -> str:
    summary = report.get("summary") or {}
    mode = "closeout" if report.get("closeout") else "advisory"
    lines = [
        "🧪 Superloop validation",
        f"loop_id: `{report.get('loop_id')}`",
        f"mode: `{mode}`",
        f"errors: `{summary.get('errors', 0)}` warnings: `{summary.get('warnings', 0)}` legacy: `{summary.get('legacy', 0)}`",
        f"blocking: `{bool(report.get('blocking'))}`",
    ]
    findings = list(report.get("findings") or [])
    if findings:
        lines.append("")
        lines.append("Findings:")
        for item in findings[:max_findings]:
            ref = f" ({item.get('ref')})" if item.get("ref") else ""
            lines.append(f"- `{item.get('severity')}` `{item.get('code')}`: {item.get('message')}{ref}")
        if len(findings) > max_findings:
            lines.append(f"- ... {len(findings) - max_findings} more")
    return "\n".join(lines)


def _resolve_json_path(store: SuperloopStore, loop_id: str, state: dict[str, Any], key: str, fallback: str) -> Path:
    try:
        return store.resolve_loop_path(loop_id, state.get(key), fallback)
    except Exception:
        return store.loop_dir(loop_id) / fallback


def _load_json_object(path: Path, findings: list[SuperloopFinding], ref: str) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        findings.append(SuperloopFinding("error", "json_invalid", f"Invalid JSON: {exc}", ref))
        return {}
    if not isinstance(payload, dict):
        findings.append(SuperloopFinding("error", "json_not_object", "Expected JSON object.", ref))
        return {}
    return payload


def _load_json_list(path: Path, findings: list[SuperloopFinding], ref: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        findings.append(SuperloopFinding("error", "json_invalid", f"Invalid JSON: {exc}", ref))
        return []
    if not isinstance(payload, list):
        findings.append(SuperloopFinding("error", "json_not_list", "Expected JSON list.", ref))
        return []
    return [item for item in payload if isinstance(item, dict)]


def _load_jsonl_list(path: Path, findings: list[SuperloopFinding], ref: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        findings.append(SuperloopFinding("warn", "jsonl_unreadable", f"Could not read JSONL: {exc}", ref))
        return []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            findings.append(SuperloopFinding("legacy", "event_json_invalid", "Invalid event JSON line.", f"{ref}:{index}"))
            continue
        if isinstance(payload, dict):
            events.append(payload)
        else:
            findings.append(SuperloopFinding("legacy", "event_not_object", "Expected event object.", f"{ref}:{index}"))
    return events


def _validate_state(state: dict[str, Any], findings: list[SuperloopFinding], *, closeout: bool) -> None:
    status = str(state.get("status") or "")
    if status and status not in ALLOWED_LOOP_STATUSES:
        severity = "error" if closeout else "legacy"
        findings.append(SuperloopFinding(severity, "loop_status_noncontract", f"Non-contract loop status: {status}", "state.json"))
    if not state.get("loop_id"):
        findings.append(SuperloopFinding("warn", "loop_id_missing", "state.json has no loop_id.", "state.json"))


def _validate_tasks(
    store: SuperloopStore,
    loop_id: str,
    tasks: list[dict[str, Any]],
    findings: list[SuperloopFinding],
    *,
    closeout: bool,
) -> None:
    in_progress = []
    for index, task in enumerate(tasks):
        ref = str(task.get("task_id") or f"taskboard[{index}]")
        if not task.get("task_id"):
            findings.append(SuperloopFinding("warn", "task_id_missing", "Task has no task_id.", ref))
        status = str(task.get("status") or "")
        if status not in ALLOWED_TASK_STATUSES:
            severity = "error" if closeout else "legacy"
            findings.append(SuperloopFinding(severity, "task_status_noncontract", f"Non-contract task status: {status}", ref))
        if status == "in_progress":
            in_progress.append(ref)
        if status in TRUTH_CLAIM_STATUSES:
            _validate_completed_task(store, loop_id, task, findings, ref=ref, closeout=closeout)
    if len(in_progress) > 1:
        findings.append(
            SuperloopFinding("warn", "multiple_tasks_in_progress", f"Multiple tasks in progress: {', '.join(in_progress)}")
        )


def _validate_completed_task(
    store: SuperloopStore,
    loop_id: str,
    task: dict[str, Any],
    findings: list[SuperloopFinding],
    *,
    ref: str,
    closeout: bool,
) -> None:
    required_evidence = task.get("required_evidence") or []
    missing_evidence = _missing_required_evidence(task, required_evidence)
    if missing_evidence:
        severity = "error" if closeout else "warn"
        findings.append(
            SuperloopFinding(
                severity,
                "completed_task_missing_evidence",
                f"Completed task is missing required evidence: {', '.join(missing_evidence)}.",
                ref,
            )
        )

    execution_mode = str(task.get("execution_mode") or "")
    if execution_mode == "hchat_agent":
        missing = []
        if not _has_any(task, "dispatch_refs", "dispatch_evidence", "hchat_dispatch_ref", "hchat_request_id"):
            missing.append("dispatch")
        if not _has_any(task, "receipt_refs", "reply_refs", "hchat_reply_ref", "worker_report_refs", "review_report_refs"):
            missing.append("receipt")
        if missing:
            severity = "error" if closeout else "warn"
            findings.append(
                SuperloopFinding(
                    severity,
                    "hchat_task_missing_receipt",
                    f"Completed hchat_agent task is missing {', '.join(missing)} evidence.",
                    ref,
                )
            )
        elif _has_any(task, "receipt_refs", "reply_refs", "hchat_reply_ref", "worker_report_refs", "review_report_refs"):
            _validate_hchat_receipt_sources(store, loop_id, task, findings, ref=ref, closeout=closeout)


def _validate_hchat_receipt_sources(
    store: SuperloopStore,
    loop_id: str,
    task: dict[str, Any],
    findings: list[SuperloopFinding],
    *,
    ref: str,
    closeout: bool,
) -> None:
    entries = _receipt_source_entries(task)
    severity = "error" if closeout else "warn"
    if not entries:
        findings.append(
            SuperloopFinding(
                severity,
                "hchat_receipt_unverifiable",
                "Completed hchat_agent task has receipt refs but no transcript-backed receipt_sources.",
                ref,
            )
        )
        return

    owner_agent = str(task.get("owner_agent") or "").strip()
    receipt_refs = _string_list_values(task, "receipt_refs", "reply_refs", "hchat_reply_ref")
    task_id = str(task.get("task_id") or ref)
    for index, entry in enumerate(entries):
        entry_ref = f"{ref}.receipt_sources[{index}]"
        agent = str(entry.get("agent") or "").strip()
        if not agent:
            findings.append(SuperloopFinding(severity, "hchat_receipt_agent_missing", "Receipt evidence has no agent.", entry_ref))
        elif owner_agent and agent != owner_agent:
            findings.append(
                SuperloopFinding(
                    severity,
                    "hchat_receipt_agent_mismatch",
                    f"Receipt evidence agent {agent} does not match task owner {owner_agent}.",
                    entry_ref,
                )
            )

        transcript_path = str(entry.get("transcript_path") or entry.get("path") or entry.get("source") or "").strip()
        if not transcript_path:
            findings.append(
                SuperloopFinding(severity, "hchat_receipt_transcript_missing", "Receipt evidence has no transcript_path.", entry_ref)
            )
            continue

        transcript = _resolve_project_path(store, transcript_path)
        if not transcript.exists():
            findings.append(
                SuperloopFinding(
                    severity,
                    "hchat_receipt_transcript_missing",
                    f"Receipt transcript does not exist: {transcript_path}",
                    entry_ref,
                )
            )
            continue

        try:
            transcript_text = _read_receipt_transcript_window(transcript, entry)
        except Exception as exc:
            findings.append(
                SuperloopFinding(severity, "hchat_receipt_transcript_unreadable", f"Could not read receipt transcript: {exc}", entry_ref)
            )
            continue

        request_id = str(entry.get("request_id") or "").strip()
        expected_tokens = [loop_id, task_id, request_id, *receipt_refs]
        if not any(token and token in transcript_text for token in expected_tokens):
            findings.append(
                SuperloopFinding(
                    severity,
                    "hchat_receipt_transcript_mismatch",
                    "Receipt transcript window does not mention the loop id, task id, request id, or receipt ref.",
                    entry_ref,
                )
            )

        artifact_path = str(entry.get("artifact_path") or entry.get("artifact_ref") or "").strip()
        if artifact_path and not _resolve_project_path(store, artifact_path).exists():
            findings.append(
                SuperloopFinding(
                    severity,
                    "hchat_receipt_artifact_missing",
                    f"Receipt artifact does not exist: {artifact_path}",
                    entry_ref,
                )
            )


def _receipt_source_entries(task: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for key in ("receipt_sources", "receipt_evidence"):
        value = task.get(key)
        if isinstance(value, dict):
            entries.append(value)
        elif isinstance(value, list):
            entries.extend(item for item in value if isinstance(item, dict))

    receipt_source = task.get("receipt_source") or task.get("transcript_path")
    if isinstance(receipt_source, str) and receipt_source.strip():
        entry: dict[str, Any] = {
            "agent": task.get("owner_agent"),
            "transcript_path": receipt_source,
        }
        if task.get("receipt_lines") is not None:
            start_line, end_line = _parse_line_range(task.get("receipt_lines"))
            if start_line is not None:
                entry["line_start"] = start_line
            if end_line is not None:
                entry["line_end"] = end_line
        entries.append(entry)
    return entries


def _parse_line_range(value: Any) -> tuple[int | None, int | None]:
    if isinstance(value, int):
        return value, value
    raw = str(value or "").strip()
    if not raw:
        return None, None
    if "-" in raw:
        left, right = raw.split("-", 1)
    else:
        left = right = raw
    try:
        start = int(left.strip())
    except Exception:
        start = None
    try:
        end = int(right.strip())
    except Exception:
        end = start
    return start, end


def _read_receipt_transcript_window(path: Path, entry: dict[str, Any]) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    start, end = _parse_line_range(entry.get("line_start") or entry.get("line") or entry.get("lines"))
    if entry.get("line_end") is not None:
        try:
            end = int(entry["line_end"])
        except Exception:
            pass
    if start is None:
        return "\n".join(lines)
    start_index = max(start - 1, 0)
    end_index = max((end or start), start)
    return "\n".join(lines[start_index:end_index])


def _resolve_project_path(store: SuperloopStore, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return (store.root_dir.parent / path).resolve()


def _string_list_values(payload: dict[str, Any], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
        elif isinstance(value, list):
            values.extend(str(item).strip() for item in value if str(item).strip())
    return values


def _validate_issues(issues: list[dict[str, Any]], findings: list[SuperloopFinding], *, closeout: bool) -> None:
    for index, issue in enumerate(issues):
        ref = str(issue.get("issue_id") or f"issues[{index}]")
        status = str(issue.get("status") or "")
        if status and status not in ALLOWED_ISSUE_STATUSES:
            findings.append(SuperloopFinding("legacy", "issue_status_noncontract", f"Non-contract issue status: {status}", ref))
        if status == "open" and _issue_blocks_closeout(issue):
            severity = "error" if closeout else "warn"
            findings.append(SuperloopFinding(severity, "open_blocker_issue", "Open blocker issue prevents clean closeout.", ref))


def _validate_waits(waits: list[dict[str, Any]], findings: list[SuperloopFinding], *, closeout: bool) -> None:
    for index, wait in enumerate(waits):
        ref = str(wait.get("wait_id") or f"waits[{index}]")
        status = str(wait.get("status") or "")
        if status and status not in ALLOWED_WAIT_STATUSES:
            findings.append(SuperloopFinding("legacy", "wait_status_noncontract", f"Non-contract wait status: {status}", ref))
        if status in {"pending", "open"}:
            severity = "error" if closeout else "warn"
            findings.append(SuperloopFinding(severity, "open_wait", "Open wait prevents closeout.", ref))


def _validate_events(events: list[dict[str, Any]], findings: list[SuperloopFinding]) -> None:
    missing_refs: list[str] = []
    for index, event in enumerate(events, start=1):
        actor = event.get("actor")
        ref = str(event.get("event_id") or f"events.jsonl:{index}")
        if not isinstance(actor, dict) or not actor:
            missing_refs.append(ref)
            continue
        if not str(actor.get("agent") or "").strip():
            missing_refs.append(ref)
    if missing_refs:
        shown = ", ".join(missing_refs[:5])
        suffix = f" (+{len(missing_refs) - 5} more)" if len(missing_refs) > 5 else ""
        findings.append(
            SuperloopFinding(
                "legacy",
                "event_actor_missing",
                f"Event ledger has missing actor attribution: {shown}{suffix}",
                "events.jsonl",
            )
        )


def _validate_closeout_shape(
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    waits: list[dict[str, Any]],
    findings: list[SuperloopFinding],
    *,
    closeout: bool,
) -> None:
    if not closeout:
        return
    unfinished = [
        str(task.get("task_id") or task.get("title") or "unknown")
        for task in tasks
        if task.get("status") not in {"completed", "skipped"}
    ]
    if unfinished:
        findings.append(
            SuperloopFinding("error", "unfinished_tasks", f"Closeout has unfinished tasks: {', '.join(unfinished[:8])}")
        )
    if any(wait.get("status") in {"pending", "open"} for wait in waits):
        state_status = str(state.get("status") or "")
        if state_status == "completed":
            findings.append(SuperloopFinding("error", "completed_with_open_waits", "Loop is completed while waits remain open."))
    if any(issue.get("status") == "open" and _issue_blocks_closeout(issue) for issue in issues):
        findings.append(SuperloopFinding("error", "completed_with_blockers", "Loop has open blocker issues."))


def _has_evidence(task: dict[str, Any]) -> bool:
    return _has_any(
        task,
        "artifact_refs",
        "evidence_refs",
        "completion_evidence",
        "evidence",
        "worker_report_refs",
        "review_report_refs",
    )


def _missing_required_evidence(task: dict[str, Any], required_evidence: Any) -> list[str]:
    if not isinstance(required_evidence, list):
        return []
    missing: list[str] = []
    for raw_item in required_evidence:
        item = str(raw_item or "").strip()
        if not item:
            continue
        key = item.lower()
        if "dispatch" in key:
            present = _has_any(task, "dispatch_refs", "dispatch_evidence", "hchat_dispatch_ref", "hchat_request_id")
        elif "receipt" in key or "reply" in key:
            present = _has_any(task, "receipt_refs", "reply_refs", "hchat_reply_ref", "worker_report_refs", "review_report_refs")
        elif "artifact" in key or "file" in key:
            present = _has_any(task, "artifact_refs", "artifacts")
        elif "validation" in key:
            present = _has_any(task, "validation_report", "validation_refs", "evidence_refs", "artifact_refs")
        elif "closeout" in key:
            present = _has_any(task, "closeout_result", "closeout_refs", "evidence_refs", "artifact_refs")
        else:
            present = _has_evidence(task)
        if not present:
            missing.append(item)
    return missing


def _has_any(payload: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, list) and value:
            return True
        if isinstance(value, dict) and value:
            return True
    return False


def _issue_blocks_closeout(issue: dict[str, Any]) -> bool:
    if issue.get("blocks_closeout") is True:
        return True
    severity = str(issue.get("severity") or "").lower()
    if severity in {"blocker", "critical", "high"}:
        return True
    title = str(issue.get("title") or "").lower()
    return "blocker" in title
