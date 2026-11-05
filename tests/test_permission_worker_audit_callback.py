"""The real Worker callback preserves the original durable/canonical audit owner."""
from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from orchestrator.her_v2.audit import DurableAuditLog
from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator
from orchestrator.her_v2.session_store import HerSessionStoreError
from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.her_callbacks import WorkerHerCallbacks
from orchestrator.permissions.models import RuntimeExecutionBinding
from tests.test_permission_pao_fence import setup_run


def environment(tmp_path, *, fail_owner=False):
    store, session, accepted, fence, context = setup_run(tmp_path)
    coordinator = HerBackendSessionCoordinator(tmp_path / "original-her")
    encoded, _ = coordinator.prepare_transport(
        session_id="original-session", sections=[], resources=[], user_message="Original request",
        request_id=accepted.request_id, message_id=accepted.message_id,
        instance_id=context.instance_id, agent_id=context.agent_id, owner_id=context.owner_id,
        hashi_conversation_id=session["session_id"], context_generation=1, workzone_identity="workzone")
    turn = coordinator.accept(encoded)
    context = replace(context, turn_id=turn.turn_id)
    binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")
    sequences = []
    def canonical(record):
        if fail_owner:
            raise OSError("original canonical owner is unavailable")
        sequences.append(coordinator.record_runtime_event(turn, record))
    primary = tmp_path / "original-audit.jsonl"
    fallback = tmp_path / "original-fallback.jsonl"
    audit = DurableAuditLog(primary, fallback, canonical_observer=canonical)
    owned = SimpleNamespace(binding=binding, adapter=SimpleNamespace(_audit_log=audit), accepted=turn)
    controller = SimpleNamespace(_turns={binding.request_id: owned}, service=SimpleNamespace(store=store))
    records = []
    class Capture:
        def append(self, record):
            records.append(record)
            return "captured-host-record"
    host = DurableAuditLog(primary_writer=Capture(), fallback_writer=Capture()).for_execution_epoch(context.execution_epoch)
    host.append(event_id="observed-runtime-start", turn_id=turn.turn_id,
                request_ref="hashi-request:" + binding.request_id,
                stage="execution", role="runtime", event="turn_started",
                payload={"intent": "original", "password": "synthetic-secret"})
    return WorkerHerCallbacks(controller), binding, records[0], primary, fallback, sequences


@pytest.mark.asyncio
async def test_real_worker_callback_reaches_canonical_owner_and_replays_without_duplicate(tmp_path):
    callbacks, binding, record, primary, fallback, sequences = environment(tmp_path)
    reference = await callbacks.audit_record(binding=binding, record=record)
    assert reference
    assert await callbacks.audit_record(binding=binding, record=record)
    rows = [json.loads(line) for line in primary.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert len(sequences) == 2 and sequences[0] == sequences[1] and sequences[0] > 0
    assert rows[0]["event_id"] == record["event_id"]
    assert rows[0]["payload"]["execution_epoch"] == binding.context.execution_epoch
    assert rows[0]["payload"]["password"] == "[REDACTED]"
    assert not fallback.exists()
    with pytest.raises(HerSessionStoreError, match="immutable facts"):
        await callbacks.audit_record(binding=binding,
            record={**record, "payload": {**record["payload"], "intent": "changed"}})
    assert len(primary.read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.asyncio
async def test_real_owner_failure_never_acknowledges_or_uses_another_audit_spool(tmp_path):
    callbacks, binding, record, primary, fallback, _ = environment(tmp_path, fail_owner=True)
    with pytest.raises(OSError, match="original canonical owner"):
        await callbacks.audit_record(binding=binding, record=record)
    assert not primary.exists() and not fallback.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("turn_id", "another-turn"), ("request_ref", "hashi-request:another"), ("format", "unregistered")])
async def test_foreign_record_never_reaches_the_original_writer(tmp_path, field, value):
    callbacks, binding, record, primary, fallback, sequences = environment(tmp_path)
    with pytest.raises(PermissionDenied):
        await callbacks.audit_record(binding=binding, record={**record, field: value})
    assert not primary.exists() and not fallback.exists() and not sequences
# HASHI-Fingerprint: hfp_da98f416086645018118657409ebbfc1
