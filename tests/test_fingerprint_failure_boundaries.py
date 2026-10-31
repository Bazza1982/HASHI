"""Crash, concurrency, Windows and failure evidence through real file tools."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from orchestrator.fingerprint.execution import observe
from orchestrator.fingerprint.manifest import read_manifest
from orchestrator.fingerprint.recovery import repair
from orchestrator.fingerprint.service import FingerprintAudit
from tests.test_fingerprint_execution import registry_at, events
from tests.test_fingerprint_admission import runtime_at

AUTH = {"actor": "test", "purpose": "crash recovery verification", "allow_raw_audit": True}


@pytest.mark.parametrize("fault", ["effect_renderer", "partial_marker"])
@pytest.mark.asyncio
async def test_after_write_effect_renderer_failure_preserves_business_success(tmp_path, monkeypatch, fault):
    registry, store, work = registry_at(tmp_path)
    import tools.effect_receipts as effects
    if fault == "effect_renderer":
        monkeypatch.setattr(effects, "observed_file_effect", lambda *a: (_ for _ in ()).throw(OSError("projection failed")))
        name, content = "saved.txt", "once"
    else:
        from orchestrator.fingerprint import markers
        def partial(path, observation, artifact_id):
            path.write_bytes(b"partial-marker")
            raise OSError("marker write interrupted")
        monkeypatch.setattr(markers, "mark", partial)
        name, content = "saved.py", "x=1\n"
    result = await registry.execute("file_write", {"path": name, "content": content}, tool_call_id="write")
    assert not result.is_error
    if fault == "effect_renderer":
        assert (work / name).read_text() == content
        assert result.details["fingerprint"]["effect_projection_status"] == "unavailable"
    else:
        assert (work / name).read_bytes() == b"partial-marker"
        assert result.details["fingerprint"]["evidence_status"] == "pending_reconciliation"
        assert result.details.get("effect_receipt") is None
        assert result.details["fingerprint"]["after"]["state"] == "unknown"
    assert len(events(store, "fingerprint_prepared")) == 1


@pytest.mark.asyncio
async def test_invalid_policy_never_blocks_unrelated_read(tmp_path):
    registry, store, work = registry_at(tmp_path)
    (work / "saved.txt").write_text("readable")
    registry.audit_context["fingerprint_policy"] = {"schema": 9000}
    read = await registry.execute("file_read", {"path": "saved.txt"}, tool_call_id="read")
    assert not read.is_error
    write = await registry.execute("file_write", {"path": "no.txt", "content": "no"}, tool_call_id="write")
    assert write.is_error and not (work / "no.txt").exists()


@pytest.mark.asyncio
async def test_external_effect_mismatch_is_unattributed_and_does_not_inject_source(tmp_path, monkeypatch):
    registry, store, work = registry_at(tmp_path)
    async def external_writer(*a, **k):
        (work / "code.py").write_text("# human edit\nhuman = True\n")
        return "OK"
    monkeypatch.setattr(registry, "_dispatch_observed", external_writer)
    result = await registry.execute("file_write", {"path": "code.py", "content": "agent = True\n"}, tool_call_id="write")
    assert not result.is_error
    assert result.details["fingerprint"]["evidence_grade"] == "interval_observation"
    assert not result.details["fingerprint"]["coverage"]["expected_content_matches"]
    assert b"HASHI-Fingerprint:" not in (work / "code.py").read_bytes()
    assert result.details.get("effect_receipt") is None


@pytest.mark.asyncio
async def test_same_file_agents_serialize_and_repair_never_rewinds_latest(tmp_path):
    first, store, work = registry_at(tmp_path)
    second, other_store, _ = registry_at(tmp_path, agent="other")
    await asyncio.gather(
        first.execute("file_write", {"path": "shared.json", "content": "{}"}, tool_call_id="one"),
        second.execute("file_write", {"path": "shared.json", "content": "{}"}, tool_call_id="two"))
    before = read_manifest(work)
    entry = before["artifacts"]["shared.json"]
    assert len(entry["receipts"]) == 2
    latest = entry["latest_receipt"]
    last = before["receipts"][latest]
    all_records = [before["receipts"][event] for event in entry["receipts"]]
    assert {r["identity"]["agent_id"] for r in all_records} == {"agent", "other"}
    repair(FingerprintAudit(store), work, authorization=AUTH)
    repair(FingerprintAudit(other_store), work, authorization=AUTH)
    assert read_manifest(work)["artifacts"]["shared.json"]["latest_receipt"] == latest


@pytest.mark.asyncio
async def test_external_manifest_conflict_is_preserved_and_only_marker_is_pending(tmp_path, monkeypatch):
    registry, store, work = registry_at(tmp_path)
    await registry.execute("file_write", {"path": "first.json", "content": "{}"}, tool_call_id="first")
    from orchestrator.fingerprint import manifest
    real_atomic = manifest.atomic_projection
    def conflicting(path, payload, **kwargs):
        if path.name == "manifest.json":
            path.write_text("external changed evidence")
        return real_atomic(path, payload, **kwargs)
    monkeypatch.setattr(manifest, "atomic_projection", conflicting)
    result = await registry.execute("file_write", {"path": "second.json", "content": "{}"}, tool_call_id="second")
    assert not result.is_error
    assert result.details["fingerprint"]["marker_status"] == "pending"
    assert (work / ".hashi-fingerprint/manifest.json").read_text() == "external changed evidence"


@pytest.mark.parametrize("stage", ["prepared", "write", "receipt", "manifest"])
def test_process_crash_recovery_only_rebuilds_evidence(tmp_path, stage):
    script = r"""
import asyncio,os,sys
from pathlib import Path
from tests.test_fingerprint_execution import registry_at
from orchestrator.fingerprint.service import FingerprintAudit
from orchestrator.fingerprint import manifest
root,stage=Path(sys.argv[1]),sys.argv[2]
registry,store,work=registry_at(root)
real_append=FingerprintAudit.append
def append(self,kind,payload,**kwargs):
    event=real_append(self,kind,payload,**kwargs)
    if (stage=='prepared' and kind=='fingerprint_prepared') or (stage=='receipt' and kind=='fingerprint_receipt'):
        os._exit(7)
    return event
FingerprintAudit.append=append
real_dispatch=registry._dispatch_observed
async def dispatch(*args,**kwargs):
    result=await real_dispatch(*args,**kwargs)
    (root/'business-count').write_text('1')
    if stage=='write':os._exit(7)
    return result
registry._dispatch_observed=dispatch
real_project=manifest.project
def project(*args,**kwargs):
    result=real_project(*args,**kwargs)
    if stage=='manifest':os._exit(7)
    return result
manifest.project=project
asyncio.run(registry.execute('file_write',{'path':'saved.json','content':'{}'},tool_call_id='once'))
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path), stage],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
    assert result.returncode == 7, result.stderr
    registry, store, work = registry_at(tmp_path)
    target = work / "saved.json"
    original = target.read_bytes() if target.exists() else None
    result = repair(FingerprintAudit(store), work, authorization=AUTH)
    assert result and result[0]["status"] == "projected"
    assert (target.read_bytes() if target.exists() else None) == original
    assert len(events(store, "fingerprint_prepared")) == 1
    if stage != "prepared":
        assert (tmp_path / "business-count").read_text() == "1"


@pytest.mark.parametrize("value", ["not-json", "[]", '{"schema":1,"enabled":true}'])
def test_corrupt_persisted_run_policy_never_silently_becomes_off(tmp_path, value):
    runtime = runtime_at(tmp_path, True)
    store = runtime.session_store
    session = store.create_session(owner_id="owner", agent_id="agent")
    accepted = store.accept_run(session_id=session["session_id"], owner_id="owner", agent_id="agent",
        request_id="request", text="hello", source="api", idempotency_key="key")
    with store._connection() as connection:
        connection.execute("UPDATE runs SET fingerprint_policy_json=? WHERE run_id=?", (value, accepted.run_id))
    with pytest.raises((ValueError, json.JSONDecodeError)):
        store.get_run(accepted.run_id)


def test_large_file_hashes_stream_without_claiming_a_bounded_readback(tmp_path):
    path = tmp_path / "large.bin"
    data = b"x" * (9 * 1024 * 1024)
    path.write_bytes(data)
    observed = observe(path)
    assert observed.version.sha256 == hashlib.sha256(data).hexdigest()
    assert observed.data is None and observed.version.size == len(data)


@pytest.mark.asyncio
async def test_case_and_long_windows_path_use_actual_file_identity(tmp_path):
    registry, store, work = registry_at(tmp_path)
    relative = "/".join(["long-component-1234567890"] * 12 + ["CaseFile.json"])
    result = await registry.execute("file_write", {"path": relative, "content": "{}"}, tool_call_id="long")
    assert not result.is_error
    assert len(str(work / relative)) > 260
    assert (work / relative).read_bytes() == b"{}"
    if os.name == "nt":
        alternate = relative.replace("CaseFile", "casefile")
        changed = await registry.execute("file_write", {"path": alternate, "content": '{"x":1}'}, tool_call_id="case")
        assert not changed.is_error
        assert changed.details["fingerprint"]["before"]["state"] == "present"


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing denial is a native Windows contract")
@pytest.mark.asyncio
async def test_windows_exclusive_file_occupancy_blocks_before_business_write(tmp_path):
    import ctypes
    from ctypes import wintypes
    registry, store, work = registry_at(tmp_path)
    path = work / "occupied.txt"
    path.write_text("keep")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value
    try:
        denied = await registry.execute("file_write", {"path": "occupied.txt", "content": "overwrite"}, tool_call_id="locked")
        assert denied.is_error
    finally:
        kernel.CloseHandle(handle)
    assert path.read_text() == "keep"


@pytest.mark.asyncio
async def test_cancellation_during_marker_retains_resource_lease_until_finalization(tmp_path, monkeypatch):
    import threading
    from orchestrator.fingerprint import markers
    registry, store, work = registry_at(tmp_path)
    second, _, _ = registry_at(tmp_path, agent="other")
    entered, release = threading.Event(), threading.Event()
    second_started = asyncio.Event()
    real_mark = markers.mark
    def held_mark(path, observation, artifact_id):
        if not entered.is_set():
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test marker not released")
        return real_mark(path, observation, artifact_id)
    monkeypatch.setattr(markers, "mark", held_mark)
    real_dispatch = second._dispatch_observed
    async def track(*args, **kwargs):
        second_started.set()
        return await real_dispatch(*args, **kwargs)
    monkeypatch.setattr(second, "_dispatch_observed", track)
    first_task = asyncio.create_task(registry.execute("file_write", {"path": "shared.py", "content": "x=1\n"}, tool_call_id="first"))
    assert await asyncio.to_thread(entered.wait, 3)
    first_task.cancel()
    other_task = asyncio.create_task(second.execute("file_write", {"path": "shared.py", "content": "x=2\n"}, tool_call_id="second"))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(second_started.wait(), .5)
        assert not first_task.done()
    finally:
        release.set()
        results = await asyncio.gather(first_task, other_task, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert not results[1].is_error
    assert (work / "shared.py").read_text().startswith("x=2\n")
