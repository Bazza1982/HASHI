"""Evidence adapters for paths which are not controlled HASHI file writes.

A reported edit is not proof of authorship or of a completed write. No git-diff
attribution, recursive repository scan, background watcher or permission bypass.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
import uuid

from .execution import Observation, observe, output_facts, target_for, finish_owned
from .models import FileVersion, FingerprintPolicy, RunIdentity, relative_target
from .runtime import context_identity, context_policy
from .service import FingerprintAudit, utc_now


def matrix() -> dict:
    return {
        "controlled_file_tools": "before/after exact bytes under participating resource lease",
        "shell": "execution window; only explicitly configured readable targets observed",
        "native_engine": "received Engine file events only; no complete execution or author guarantee",
        "supervised_files": "existing permission evidence retained; artifact capture not qualified",
        "external_editors": "not locked; conflicting or unobserved changes remain unattributed",
        "background_writes": "may continue after foreground completion; coverage incomplete",
    }


async def dispatch_shell(registry, tool_name, arguments, *, tool_call_id):
    from tools.builtins import BuiltinExecutionResult

    context = registry._effective_audit_context()
    policy = context_policy(context)
    if not policy.enabled:
        return await registry._dispatch_observed(tool_name, arguments, tool_call_id=tool_call_id)
    audit = FingerprintAudit(registry.canonical_audit)
    identity = context_identity(context)
    audit.validate_identity(identity)
    operation_id = uuid.uuid4().hex
    call_id = tool_call_id or uuid.uuid4().hex
    # This is trusted observer configuration, NOT command text or a model argument.
    requested = context.get("fingerprint_observation_paths", ())
    targets = requested if isinstance(requested, (tuple, list)) else ()
    observations, gaps = [], []
    for raw in targets[:32]:
        try:
            target = target_for(registry, {"path": raw})
            snapshot = await asyncio.to_thread(observe, target[0])
            observations.append((target, snapshot))
        except (OSError, ValueError):
            gaps.append("target_unavailable")
    if not observations:
        gaps.append("no_bounded_file_observation")
    prepared = {"operation_id": operation_id, "identity": asdict(identity),
        "tool_call_id": call_id, "tool_name": tool_name, "policy_version": policy.version,
        "started_at": utc_now(), "coverage": matrix(), "gaps": gaps}
    evidence = "persisted"
    try:
        await asyncio.to_thread(audit.append, "fingerprint_execution_started", prepared, request_id=identity.request_id)
    except Exception:
        evidence = "unavailable"
    error = None
    try:
        result = await registry._dispatch_observed(tool_name, arguments, tool_call_id=call_id)
        output, details, business = output_facts(result)
    except BaseException as exc:
        error = exc
        output, details, business = "", dict(getattr(exc, "hashi_tool_details", {}) or {}), "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
    async def finalize():
        cleanup = details.get("foreground_cleanup") or {}
        background_pending = (cleanup.get("group_alive") is True or cleanup.get("status") == "cleanup_failed"
                              or cleanup.get("process_reaped") is False or "background_job_id" in details)
        records = []
        for (path, root, relative, root_id, artifact_id), before in observations:
            try:
                after = await asyncio.to_thread(observe, path)
                if before.version == after.version:
                    continue
                record = await asyncio.to_thread(audit.record_operation,
                    policy=policy, identity=identity, tool_call_id=call_id, tool_name=tool_name,
                    artifact_id=artifact_id, root_id=root_id, relative_path=relative,
                    action="delete" if after.version.state == "absent" else "create" if before.version.state == "absent" else "modify",
                    before=before.version, after=after.version, business_status=business,
                    operation_id=operation_id + "-" + uuid.uuid4().hex[:8],
                    evidence_grade="interval_observation", marker_status="pending",
                    coverage={"complete": False, "background_pending": background_pending,
                              "attribution": "unknown", "scope": "configured targets only"})
                # An uncertainty-labelled source index is allowed; never add an author comment.
                from .manifest import project
                await asyncio.to_thread(project, root, record, file_id=after.file_id)
                records.append(record["event_id"])
            except Exception:
                gaps.append("observation_or_projection_unavailable")
        window = {**prepared, "business_status": business, "evidence_status": evidence,
            "observed_at": utc_now(), "receipt_event_ids": records,
            "coverage_complete": False, "background_pending": background_pending}
        try:
            window["event_id"] = await asyncio.to_thread(audit.append, "fingerprint_execution_window", window,
                                                         request_id=identity.request_id)
        except Exception:
            window["evidence_status"] = "pending_reconciliation"
        details["fingerprint"] = window

    _unused, cancellation = await finish_owned(finalize())
    if error is None and cancellation is not None:
        error = cancellation
    if error is not None:
        error.hashi_tool_details = {**getattr(error, "hashi_tool_details", {}), **details}
        raise error
    return BuiltinExecutionResult(output, details)


async def observe_engine_event(runtime, item, event):
    """One transparent PAO callback; records only actually received file events."""
    if getattr(event, "kind", None) != "file_edit":
        return None
    metadata = getattr(item, "request_metadata", None) or {}
    policy = FingerprintPolicy.from_snapshot(metadata.get("fingerprint_policy"))
    if not policy.enabled:
        return None
    identity = RunIdentity(str(runtime.global_config.instance_id), str(runtime.name),
        str(getattr(item, "session_id", "") or ""), str(getattr(item, "run_id", "") or ""), str(item.request_id))
    audit = FingerprintAudit(getattr(runtime, "canonical_audit", None))
    audit.validate_identity(identity)
    event_metadata = getattr(event, "metadata", None) or {}
    raw_paths = event_metadata.get("file_paths") or [getattr(event, "file_path", "")]
    if not isinstance(raw_paths, (tuple, list)):
        raw_paths = []
    relative_paths, omitted = [], max(0, len(raw_paths) - 32)
    for raw in raw_paths[:32]:
        try:
            relative_paths.append(relative_target(raw))
        except ValueError:
            omitted += 1
    payload = {"schema": 1, "operation_id": uuid.uuid4().hex, "identity": asdict(identity),
        "engine_event_id": str(getattr(event, "event_id", "") or ""),
        "evidence_grade": "engine_reported", "business_status": "unknown",
        "marker_status": "not_applied", "policy_version": policy.version,
        "reported_paths": relative_paths, "omitted_path_count": omitted,
        "observed_at": utc_now(), "coverage": {
            "complete": False, "scope": "received file events only", "before_state": "unknown",
            "write_completion": "not_confirmed", "supervised_handle_reopened": False,
            "native_paths_without_events": "not_covered", "author_authentication": "not_verified"}}
    return await asyncio.to_thread(audit.append, "fingerprint_engine_report", payload, request_id=identity.request_id)
