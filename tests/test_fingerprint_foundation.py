from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

from orchestrator.canonical_audit import CanonicalAuditStore, BufferedCanonicalAuditWriter
from orchestrator.config_admin import ConfigAdmin
from orchestrator.config_json import ConfigConflictError, ConfigDurabilityError, read_config_json, write_config_json
from orchestrator.fingerprint.models import FingerprintPolicy, RunIdentity, FileVersion
from orchestrator.fingerprint.service import FingerprintAudit, FingerprintUnavailable


def store_at(tmp_path):
    return CanonicalAuditStore(tmp_path / "home", instance_id="TEST", agent_id="agent")


def identity():
    return RunIdentity("TEST", "agent", "session", "run", "request")


def admin_at(tmp_path):
    path = tmp_path / "agents.json"
    write_config_json(path, {"global": {"unrelated": "retain"}, "agents": []})
    return ConfigAdmin(SimpleNamespace(config_path=path))


def test_missing_policy_stays_off_and_does_not_modify_config(tmp_path):
    admin = admin_at(tmp_path)
    before = admin.paths.config_path.read_bytes()
    snapshot = admin.read_fingerprint_policy()
    assert not snapshot.policy.enabled
    assert snapshot.policy.export_content is False
    assert admin.paths.config_path.read_bytes() == before
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.policy.enabled = True


@pytest.mark.parametrize("value", [
    {"enabled": "false"}, {"enabled": 1}, None,
    {"enabled": True, "marking": "strict"},
    {"export_content": "true"}, {"unrecognised": True},
])
def test_policy_rejects_ambiguous_or_unsupported_values(value):
    with pytest.raises(ValueError):
        FingerprintPolicy.from_mapping(value)


def test_policy_snapshot_is_json_roundtrippable_and_detects_version_mismatch():
    policy = FingerprintPolicy.from_mapping({"enabled": True})
    snapshot = json.loads(json.dumps(policy.snapshot()))
    assert FingerprintPolicy.from_snapshot(snapshot) == policy
    snapshot["enabled"] = False
    with pytest.raises(ValueError):
        FingerprintPolicy.from_snapshot(snapshot)


def test_disabled_service_requires_no_identity_audit_or_observation():
    audit = FingerprintAudit(None)
    assert audit.record_operation(
        policy=FingerprintPolicy(), identity=None, tool_call_id="", tool_name="file_write",
        artifact_id="", root_id="", relative_path="", action="modify",
        before=FileVersion(), after=FileVersion(), business_status="completed",
    ) is None


def test_config_change_retains_other_fields_and_has_durable_admin_audit(tmp_path):
    admin = admin_at(tmp_path)
    store = store_at(tmp_path)
    original = admin.read_fingerprint_policy()
    changed = admin.write_fingerprint_policy(
        FingerprintPolicy(enabled=True), expected_revision=original.revision,
        actor_id="owner", audit=store,
    )
    assert changed.policy.enabled
    raw = read_config_json(admin.paths.config_path)
    assert raw["global"]["unrelated"] == "retain"
    assert raw.revision == changed.revision
    events = [json.loads(line)["event"] for line in store.events_path.read_text().splitlines()]
    assert [e["event_type"] for e in events] == ["fingerprint_policy_prepared", "fingerprint_policy_changed"]
    assert all(e["payload"]["actor_id"] == "owner" for e in events)
    assert events[-1]["payload"]["config_revision"] == changed.revision


def test_stale_config_revision_never_overwrites_new_settings(tmp_path):
    admin = admin_at(tmp_path)
    snapshot = admin.read_fingerprint_policy()
    raw = read_config_json(admin.paths.config_path)
    raw["global"]["unrelated"] = "newer"
    write_config_json(admin.paths.config_path, raw)
    with pytest.raises(ConfigConflictError):
        admin.write_fingerprint_policy(
            FingerprintPolicy(enabled=True), expected_revision=snapshot.revision,
            actor_id="owner", audit=store_at(tmp_path),
        )
    assert admin.read_fingerprint_policy().policy.enabled is False
    assert read_config_json(admin.paths.config_path)["global"]["unrelated"] == "newer"


def test_preparation_audit_failure_does_not_apply_configuration(tmp_path, monkeypatch):
    admin = admin_at(tmp_path)
    snapshot = admin.read_fingerprint_policy()
    store = store_at(tmp_path)
    monkeypatch.setattr(store, "record", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(FingerprintUnavailable):
        admin.write_fingerprint_policy(
            FingerprintPolicy(enabled=True), expected_revision=snapshot.revision,
            actor_id="owner", audit=store,
        )
    assert not admin.read_fingerprint_policy().policy.enabled


def test_post_publish_audit_failure_is_committed_and_must_not_be_replayed(tmp_path, monkeypatch):
    admin = admin_at(tmp_path)
    snapshot = admin.read_fingerprint_policy()
    store = store_at(tmp_path)
    real_record = store.record

    def fail_result(kind, *args, **kwargs):
        if kind == "fingerprint_policy_changed":
            raise OSError("write failed")
        return real_record(kind, *args, **kwargs)

    monkeypatch.setattr(store, "record", fail_result)
    with pytest.raises(ConfigDurabilityError) as caught:
        admin.write_fingerprint_policy(
            FingerprintPolicy(enabled=True), expected_revision=snapshot.revision,
            actor_id="owner", audit=store,
        )
    assert caught.value.committed is True
    assert admin.read_fingerprint_policy().policy.enabled is True


@pytest.mark.parametrize("buffered", [False, True])
def test_operation_receipt_reopens_from_actual_canonical_store(tmp_path, buffered):
    store = store_at(tmp_path)
    writer = BufferedCanonicalAuditWriter(store) if buffered else store
    service = FingerprintAudit(writer)
    receipt = service.record_operation(
        policy=FingerprintPolicy(enabled=True), identity=identity(), tool_call_id="call",
        tool_name="file_write", artifact_id="artifact", root_id="root", relative_path="file.py",
        action="modify", before=FileVersion("present", "a" * 64),
        after=FileVersion("present", "b" * 64), business_status="completed",
    )
    if buffered:
        writer.close()
    reopened = store_at(tmp_path)
    events = [json.loads(line)["event"] for line in reopened.events_path.read_text().splitlines()]
    assert receipt["evidence_status"] == "persisted"
    assert receipt["marker_status"] == "not_implemented"
    event = events[-1]
    assert event["event_id"] == receipt["event_id"]
    assert event["event_type"] == "fingerprint_receipt"
    assert event["instance_id"] == "TEST" and event["agent_id"] == "agent"
    assert event["payload"]["identity"]["run_id"] == "run"
    assert event["payload"]["identity"]["session_id"] == "session"
    assert event["payload"]["tool_call_id"] == "call"
    assert event["payload"]["after"]["sha256"] == "b" * 64
    assert receipt["event_id"]


def test_buffered_event_is_not_reported_persisted_when_barrier_fails(tmp_path, monkeypatch):
    writer = BufferedCanonicalAuditWriter(store_at(tmp_path))
    real_flush = writer.flush
    monkeypatch.setattr(writer, "flush", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("barrier")))
    with pytest.raises(FingerprintUnavailable):
        FingerprintAudit(writer).append("fingerprint_receipt", {}, request_id="request")
    monkeypatch.setattr(writer, "flush", real_flush)
    writer.close()


def test_missing_or_mismatched_identity_cannot_create_attributed_receipt(tmp_path):
    service = FingerprintAudit(store_at(tmp_path))
    with pytest.raises(ValueError):
        RunIdentity("TEST", "agent", "session", "", "request")
    with pytest.raises(FingerprintUnavailable):
        service.record_operation(
            policy=FingerprintPolicy(enabled=True),
            identity=RunIdentity("OTHER", "agent", "session", "run", "request"),
            tool_call_id="call", tool_name="file_write", artifact_id="artifact",
            root_id="root", relative_path="f.py", action="modify",
            before=FileVersion(), after=FileVersion(), business_status="completed",
        )


@pytest.mark.parametrize("relative_path", ["/private/file", "../secret", "C:\\private\\file"])
def test_receipt_target_cannot_publish_an_absolute_or_escaping_path(tmp_path, relative_path):
    with pytest.raises(ValueError):
        FingerprintAudit(store_at(tmp_path)).record_operation(
            policy=FingerprintPolicy(enabled=True), identity=identity(), tool_call_id="call",
            tool_name="file_write", artifact_id="artifact", root_id="root",
            relative_path=relative_path, action="modify",
            before=FileVersion(), after=FileVersion(), business_status="completed",
        )
