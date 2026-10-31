"""Actual ToolRegistry file effects and durability failures."""
from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from orchestrator.canonical_audit import CanonicalAuditStore
from orchestrator.fingerprint.models import FingerprintPolicy
from tools.registry import ToolRegistry


def registry_at(tmp_path, *, enabled=True, agent="agent"):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    store = CanonicalAuditStore(tmp_path / "home", instance_id="TEST", agent_id=agent)
    registry = ToolRegistry(["*"], work, work, {}, canonical_audit=store,
        audit_context={"instance_id": "TEST", "agent_name": agent,
            "hashi_session_id": "session", "hashi_run_id": "run", "request_id": "request",
            "fingerprint_policy": FingerprintPolicy(enabled=enabled).snapshot()})
    return registry, store, work


def events(store, kind):
    return [event for event in store.read_events({"actor": "test", "purpose": "verification", "allow_raw_audit": True})
            if event["event_type"] == kind]


@pytest.mark.asyncio
async def test_off_has_identical_bytes_and_no_fingerprint_events_or_namespace(tmp_path, monkeypatch):
    registry, store, work = registry_at(tmp_path, enabled=False)
    result = await registry.execute("file_write", {"path": "hello.txt", "content": "hello"}, tool_call_id="one")
    assert not result.is_error and (work / "hello.txt").read_bytes() == b"hello"
    assert not events(store, "fingerprint_prepared")
    assert not (work / ".hashi-fingerprint").exists()
    assert "fingerprint" not in (result.details or {})


@pytest.mark.asyncio
async def test_write_patch_and_no_change_capture_actual_versions(tmp_path):
    registry, store, work = registry_at(tmp_path)
    target = work / "hello.txt"
    target.write_bytes(b"human\n")
    original = hashlib.sha256(target.read_bytes()).hexdigest()
    written = await registry.execute("file_write", {"path": "hello.txt", "content": "human\nagent\n"}, tool_call_id="write")
    assert not written.is_error
    receipt = written.details["fingerprint"]
    assert receipt["before"]["sha256"] == original
    assert receipt["after"]["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert receipt["evidence_status"] == "persisted"
    assert receipt["business_status"] == "completed"
    same = await registry.execute("file_write", {"path": "hello.txt", "content": "human\nagent\n"}, tool_call_id="same")
    assert same.details["fingerprint"]["action"] == "no_change"
    patched = await registry.execute("apply_patch", {"path": "hello.txt",
        "patch": "--- a/hello.txt\n+++ b/hello.txt\n@@ -1,2 +1,2 @@\n human\n-agent\n+new\n"}, tool_call_id="patch")
    assert not patched.is_error
    assert target.read_text() == "human\nnew\n"
    assert len(events(store, "fingerprint_prepared")) == 3
    assert len(events(store, "fingerprint_receipt")) == 3
    assert patched.details["effect_receipt"]["revision"] == "sha256:" + patched.details["fingerprint"]["after"]["sha256"]


@pytest.mark.asyncio
async def test_missing_trusted_identity_or_preparation_failure_blocks_only_mutation(tmp_path, monkeypatch):
    registry, store, work = registry_at(tmp_path)
    registry.audit_context.pop("hashi_run_id")
    denied = await registry.execute("file_write", {"path": "no.txt", "content": "no"}, tool_call_id="write")
    assert denied.is_error and not (work / "no.txt").exists()
    (work / "read.txt").write_text("readable")
    assert not (await registry.execute("file_read", {"path": "read.txt"}, tool_call_id="read")).is_error
    registry.audit_context["hashi_run_id"] = "run"
    real_record = store.record
    def fail_prepared(kind, *args, **kwargs):
        if kind == "fingerprint_prepared":
            raise OSError("full")
        return real_record(kind, *args, **kwargs)
    monkeypatch.setattr(store, "record", fail_prepared)
    denied = await registry.execute("file_write", {"path": "no.txt", "content": "no"}, tool_call_id="second")
    assert denied.is_error and not (work / "no.txt").exists()


@pytest.mark.asyncio
async def test_post_write_audit_failure_never_makes_committed_business_retryable(tmp_path, monkeypatch):
    registry, store, work = registry_at(tmp_path)
    real_record = store.record
    def fail_receipt(kind, *args, **kwargs):
        if kind == "fingerprint_receipt":
            raise OSError("full")
        return real_record(kind, *args, **kwargs)
    monkeypatch.setattr(store, "record", fail_receipt)
    result = await registry.execute("file_write", {"path": "saved.txt", "content": "once"}, tool_call_id="write")
    assert not result.is_error and (work / "saved.txt").read_text() == "once"
    assert result.details["fingerprint"]["evidence_status"] == "pending_reconciliation"
    assert result.details["fingerprint"]["business_status"] == "completed"
    assert len(events(store, "fingerprint_prepared")) == 1


@pytest.mark.asyncio
async def test_rejected_patch_and_cancelled_partial_write_keep_honest_receipts(tmp_path, monkeypatch):
    registry, store, work = registry_at(tmp_path)
    (work / "data.txt").write_text("keep")
    rejected = await registry.execute("apply_patch", {"path": "data.txt", "patch": "--- a\n+++ b\n@@ -1 +1 @@\n-missing\n+bad\n"}, tool_call_id="reject")
    assert rejected.is_error and rejected.details["fingerprint"]["business_status"] == "failed"
    async def interrupted(tool_name, arguments, **kwargs):
        (work / "partial.txt").write_text("partial")
        raise asyncio.CancelledError()
    monkeypatch.setattr(registry, "_dispatch_observed", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await registry.execute("file_write", {"path": "partial.txt", "content": "all"}, tool_call_id="cancel")
    receipt = events(store, "fingerprint_receipt")[-1]["payload"]
    assert receipt["business_status"] == "cancelled"
    assert receipt["after"]["state"] == "present"
