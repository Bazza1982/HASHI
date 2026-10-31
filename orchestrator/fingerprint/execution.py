"""Capture controlled effects inside the existing resource lease.

Only PAO-supplied tool context is trusted. This does not lock external editors.
Supervised handle-based paths use their own evidence adapter, never these reads.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import Path
import stat
import time
import uuid

from .models import FileVersion, relative_target
from .runtime import context_identity, context_policy
from .service import FingerprintAudit, FingerprintUnavailable, utc_now

MAX_OBSERVATION_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class Observation:
    version: FileVersion
    data: bytes | None = None
    file_id: tuple | None = None


def observe(path: Path) -> Observation:
    """Stream hashes; reject unstable reads and non-regular/linked objects."""
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return Observation(FileVersion("absent"))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise FingerprintUnavailable("fingerprint requires a single regular file")
        digest = hashlib.sha256()
        parts = [] if before.st_size <= MAX_OBSERVATION_BYTES else None
        while block := stream.read(1024 * 1024):
            digest.update(block)
            if parts is not None:
                parts.append(block)
        after = os.fstat(stream.fileno())
    current = path.stat(follow_symlinks=False)
    keys = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    # Python 3.12 Windows stat() exposes creation time as ctime, while fstat()
    # can expose change time. Compare ctime only within the same API.
    path_keys = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    if keys(before) != keys(after) or path_keys(after) != path_keys(current):
        return Observation(FileVersion("conflict"))
    return Observation(FileVersion("present", digest.hexdigest(), after.st_size),
        b"".join(parts) if parts is not None else None, (after.st_dev, after.st_ino))


def target_for(registry, arguments):
    from tools.builtins import _resolve_path

    path = _resolve_path(str(arguments.get("path") or ""), registry.access_roots, registry.workspace_dir)
    roots = [Path(registry.workspace_dir).resolve(), *map(Path, registry.access_roots)]
    candidates = [root for root in roots if path != root and path.is_relative_to(root)]
    if not candidates:
        raise FingerprintUnavailable("no authorized artifact root")
    # Prefer the actual working project, then the narrowest authorized root.
    root = candidates[0] if candidates[0] == Path(registry.workspace_dir).resolve() else max(candidates, key=lambda p: len(p.parts))
    relative = relative_target(path.relative_to(root).as_posix())
    root_id = "hfr_" + hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()[:32]
    artifact_id = "hfp_" + uuid.uuid4().hex
    return path, root, relative, root_id, artifact_id


def output_facts(result):
    from tools.builtins import BuiltinExecutionResult
    output = result.output if isinstance(result, BuiltinExecutionResult) else str(result)
    details = dict(result.details) if isinstance(result, BuiltinExecutionResult) else {}
    failed = output.startswith("Error") or details.get("exit_code", 0) not in {0, None}
    return output, details, "failed" if failed else "completed"


def expected_matches(name, arguments, before, after) -> bool:
    if after.version.state != "present" or after.data is None:
        return False
    if name == "file_write":
        expected = str(arguments.get("content") or "").encode("utf-8")
        if os.linesep == "\r\n":
            expected = expected.replace(b"\n", b"\r\n")
        return expected == after.data
    if before.data is None:
        return False
    from tools.builtins import _apply_unified_diff
    try:
        ok, text = _apply_unified_diff(before.data.decode("utf-8", "surrogateescape"), str(arguments.get("patch") or ""))
        return bool(ok and text.encode("utf-8", "surrogateescape") == after.data)
    except (ValueError, UnicodeError):
        return False


async def dispatch_controlled(registry, tool_name, arguments, *, tool_call_id):
    """Called only AFTER admission and INSIDE tool_resource; OFF is a no-op."""
    context = registry._effective_audit_context()
    if tool_name in {"shell", "bash"}:
        from .coverage import dispatch_shell
        return await dispatch_shell(registry, tool_name, arguments, tool_call_id=tool_call_id)
    if tool_name not in {"file_write", "apply_patch"}:
        return await registry._dispatch_observed(tool_name, arguments, tool_call_id=tool_call_id)
    policy = context_policy(context)
    if not policy.enabled:
        return await registry._dispatch_observed(tool_name, arguments, tool_call_id=tool_call_id)
    from tools.builtins import BuiltinExecutionResult
    started = time.monotonic()
    audit = FingerprintAudit(registry.canonical_audit)
    identity = context_identity(context)
    audit.validate_identity(identity)
    call_id = tool_call_id or uuid.uuid4().hex
    path, root, relative, root_id, artifact_id = target_for(registry, arguments)
    if ".hashi-fingerprint" in path.parts or path.name.endswith(".hashi.json"):
        # Derived evidence is never recursively fingerprinted or overwritten as business data.
        raise FingerprintUnavailable("fingerprint evidence namespace is system-derived")
    before = await asyncio.to_thread(observe, path)
    if before.version.state == "conflict":
        raise FingerprintUnavailable("target changed during preparation")
    try:
        from .manifest import existing_artifact
        artifact_id = await asyncio.to_thread(existing_artifact, root, relative, before, artifact_id)
    except Exception:
        pass  # Unknown/colliding evidence must not block the authorized business write.
    operation_id = uuid.uuid4().hex
    begun = utc_now()
    prepared = {
        "schema": 1, "operation_id": operation_id, "identity": asdict(identity),
        "tool_call_id": call_id, "tool_name": tool_name, "artifact_id": artifact_id,
        "root_id": root_id, "relative_path": relative, "before": asdict(before.version),
        "policy_version": policy.version, "started_at": begun,
    }
    # No business effect may precede this durability barrier.
    prepared_id = await asyncio.to_thread(audit.append, "fingerprint_prepared", prepared, request_id=identity.request_id)
    failure = None
    business_started = time.monotonic()
    try:
        result = await registry._dispatch_observed(tool_name, arguments, tool_call_id=call_id)
        output, details, business = output_facts(result)
    except BaseException as exc:
        failure = exc
        output, details, business = "", {}, "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
    business_elapsed = time.monotonic() - business_started
    receipt = {**prepared, "business_status": business, "evidence_status": "pending_reconciliation",
               "marker_status": "pending", "prepared_event_id": prepared_id}
    after = Observation(FileVersion())
    try:
        after = await asyncio.to_thread(observe, path)
        matches = expected_matches(tool_name, arguments, before, after) if business == "completed" else False
        business_changed = before.version != after.version
        marker_status, marker_changed = "pending", False
        if matches:
            from .markers import mark
            marker_status, after, marker_changed = await asyncio.to_thread(mark, path, after, artifact_id)
        elif after.version.state == "absent":
            marker_status = "deleted"
        action = ("delete" if after.version.state == "absent" and before.version.state == "present" else
                  "create" if before.version.state == "absent" and after.version.state == "present" else
                  "no_change" if before.version == after.version else "modify")
        grade = "controlled_execution" if matches and marker_status != "conflict" else "interval_observation"
        coverage = {"external_editors_locked": False, "expected_content_matches": matches,
                    "scope": "one admitted tool operation; external processes excluded"}
        receipt = await asyncio.to_thread(audit.record_operation,
            policy=policy, identity=identity, tool_call_id=call_id, tool_name=tool_name,
            artifact_id=artifact_id, root_id=root_id, relative_path=relative, action=action,
            before=before.version, after=after.version, business_status=business,
            operation_id=operation_id, started_at=begun, prepared_event_id=prepared_id,
            evidence_grade=grade, marker_status="pending" if marker_status == "manifest" else marker_status, coverage=coverage,
            marker_changed_bytes=marker_changed, business_changed_bytes=business_changed)
        if business == "completed" and after.version.state in {"present", "absent"}:
            try:
                from .manifest import project
                await asyncio.to_thread(project, root, {**receipt, "marker_status": marker_status}, file_id=after.file_id)
                receipt["marker_status"] = marker_status
                receipt["projection_status"] = "projected"
            except Exception:
                receipt["marker_status"] = "pending"
                receipt["projection_status"] = "pending"
            try:
                await asyncio.to_thread(audit.append, "fingerprint_projection", {
                    "operation_id": operation_id, "receipt_event_id": receipt["event_id"],
                    "marker_status": receipt["marker_status"], "projection_status": receipt["projection_status"],
                }, request_id=identity.request_id)
            except FingerprintUnavailable:
                receipt["projection_audit_status"] = "unconfirmed"
    except Exception:
        # A committed business operation must not be exposed as retryable.
        receipt["after"] = asdict(after.version)
    details["fingerprint"] = {**receipt, "extra_elapsed_ms": round(max(0, time.monotonic()-started-business_elapsed)*1000, 3)}
    if after.data is not None and business == "completed":
        try:
            from tools.effect_receipts import observed_file_effect
            details["effect_receipt"] = observed_file_effect(tool_name, call_id, path, after.data)
        except Exception:
            details["fingerprint"]["effect_projection_status"] = "unavailable"
    if failure is not None:
        try:
            failure.hashi_tool_details = {**getattr(failure, "hashi_tool_details", {}), **details}
        except Exception:
            pass
        raise failure
    return BuiltinExecutionResult(output, details)


def reconcile(audit: FingerprintAudit, root: Path, *, authorization: dict) -> list[dict]:
    """Observe unresolved preparations only. Never re-execute a business tool."""
    events = audit.store.read_events(authorization)
    completed = {e["payload"].get("operation_id") for e in events
                 if e["event_type"] in {"fingerprint_receipt", "fingerprint_reconciled"}}
    results = []
    root = root.resolve()
    wanted_root = "hfr_" + hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()[:32]
    for event in events:
        item = event.get("payload", {})
        if event["event_type"] != "fingerprint_prepared" or item.get("operation_id") in completed or item.get("root_id") != wanted_root:
            continue
        path = root / relative_target(item["relative_path"])
        if not path.resolve().is_relative_to(root):
            raise FingerprintUnavailable("recovery target escaped the authorized root")
        observation = observe(path)
        payload = {**item, "prepared_event_id": event["event_id"], "after": asdict(observation.version),
                   "business_status": "unknown", "evidence_status": "reconciled_observation",
                   "marker_status": "pending", "evidence_grade": "interval_observation",
                   "observed_at": utc_now(), "coverage": {"business_replayed": False}}
        payload["event_id"] = audit.append("fingerprint_reconciled", payload, request_id=item["identity"]["request_id"])
        results.append(payload)
    return results
