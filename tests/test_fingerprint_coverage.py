"""Uncontrolled paths never become affirmative Agent author claims."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from adapters.stream_events import StreamEvent
from orchestrator.fingerprint.coverage import observe_engine_event, matrix
from orchestrator.fingerprint.models import FingerprintPolicy
from tools.builtins import BuiltinExecutionResult
from tests.test_fingerprint_execution import registry_at, events


@pytest.mark.asyncio
async def test_failed_shell_keeps_partial_effects_with_interval_grade(tmp_path, monkeypatch):
    registry, store, work = registry_at(tmp_path)
    registry.audit_context["fingerprint_observation_paths"] = ["partial.txt"]
    async def partly_failed(*args, **kwargs):
        (work / "partial.txt").write_text("partial")
        return BuiltinExecutionResult("Error: exit 1", {"exit_code": 1,
            "foreground_cleanup": {"group_alive": True}})
    monkeypatch.setattr(registry, "_dispatch_observed", partly_failed)
    result = await registry.execute("shell", {"command": "opaque command"}, tool_call_id="shell")
    assert result.is_error
    receipt = events(store, "fingerprint_receipt")[-1]["payload"]
    assert receipt["after"]["state"] == "present"
    assert receipt["evidence_grade"] == "interval_observation"
    assert receipt["coverage"]["attribution"] == "unknown"
    assert receipt["coverage"]["background_pending"] is True
    assert result.details["fingerprint"]["coverage_complete"] is False
    assert b"HASHI-Fingerprint:" not in (work / "partial.txt").read_bytes()


@pytest.mark.asyncio
async def test_shell_no_declared_targets_never_scans_worktree(tmp_path, monkeypatch):
    registry, store, work = registry_at(tmp_path)
    (work / "human.txt").write_text("leave alone")
    async def nothing(*a, **k):
        return "OK"
    from orchestrator.fingerprint import coverage
    monkeypatch.setattr(coverage, "observe", lambda *a: pytest.fail("unexpected file scan"))
    monkeypatch.setattr(registry, "_dispatch_observed", nothing)
    result = await registry.execute("shell", {"command": "opaque"}, tool_call_id="shell")
    assert not result.is_error
    assert "no_bounded_file_observation" in result.details["fingerprint"]["gaps"]
    assert not events(store, "fingerprint_receipt")


@pytest.mark.asyncio
async def test_native_file_start_event_is_reported_not_committed_or_attributed(tmp_path):
    registry, store, work = registry_at(tmp_path)
    runtime = SimpleNamespace(name="agent", global_config=SimpleNamespace(instance_id="TEST"), canonical_audit=store)
    item = SimpleNamespace(session_id="session", run_id="run", request_id="request",
        request_metadata={"fingerprint_policy": FingerprintPolicy(enabled=True).snapshot()})
    event = StreamEvent("file_edit", "Edit starting", file_path="", tool_name="Write")
    await observe_engine_event(runtime, item, event)
    report = events(store, "fingerprint_engine_report")[-1]["payload"]
    assert report["business_status"] == "unknown"
    assert report["coverage"]["write_completion"] == "not_confirmed"
    assert report["marker_status"] == "not_applied"
    event = StreamEvent("file_edit", "Reported file", metadata={"file_paths": ["code.py", "C:\\private\\secret"]})
    await observe_engine_event(runtime, item, event)
    report = events(store, "fingerprint_engine_report")[-1]["payload"]
    assert report["reported_paths"] == ["code.py"] and report["omitted_path_count"] == 1
    item.request_metadata["fingerprint_policy"] = FingerprintPolicy().snapshot()
    assert await observe_engine_event(runtime, item, event) is None
    assert len(events(store, "fingerprint_engine_report")) == 2
    assert "not qualified" in matrix()["supervised_files"]


@pytest.mark.asyncio
async def test_cancelled_shell_preserves_incomplete_cleanup_evidence(tmp_path, monkeypatch):
    import asyncio
    registry, store, work = registry_at(tmp_path)
    async def cancelled(*args, **kwargs):
        error = asyncio.CancelledError()
        error.hashi_tool_details = {"foreground_cleanup": {"group_alive": True, "process_reaped": False}}
        raise error
    monkeypatch.setattr(registry, "_dispatch_observed", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await registry.execute("shell", {"command": "opaque"}, tool_call_id="cancel")
    window = events(store, "fingerprint_execution_window")[-1]["payload"]
    assert window["background_pending"] is True and window["coverage_complete"] is False


@pytest.mark.asyncio
async def test_bounded_move_copy_and_delete_keep_distinct_binary_artifact_identities(tmp_path, monkeypatch):
    registry, store, work = registry_at(tmp_path)
    original = bytes([0, 255]) + b"signed-binary" + bytes([16])
    with pytest.raises(UnicodeDecodeError):
        original.decode("utf-8")
    (work / "old.bin").write_bytes(original)
    registry.audit_context["fingerprint_observation_paths"] = ["old.bin", "moved.bin", "copy.bin"]
    async def move_and_copy(*args, **kwargs):
        (work / "old.bin").rename(work / "moved.bin")
        (work / "copy.bin").write_bytes((work / "moved.bin").read_bytes())
        return "OK"
    monkeypatch.setattr(registry, "_dispatch_observed", move_and_copy)
    result = await registry.execute("shell", {"command": "opaque move/copy"}, tool_call_id="move")
    assert not result.is_error
    assert (work / "moved.bin").read_bytes() == original == (work / "copy.bin").read_bytes()
    from orchestrator.fingerprint.manifest import read_manifest
    manifest = read_manifest(work)
    old, moved, copied = [manifest["artifacts"][name] for name in ("old.bin", "moved.bin", "copy.bin")]
    assert old["state"] == "absent"
    assert moved["sha256"] == copied["sha256"] and moved["artifact_id"] != copied["artifact_id"]
    assert all(record["evidence_grade"] == "interval_observation" for record in manifest["receipts"].values())
