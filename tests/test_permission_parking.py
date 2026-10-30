"""Parked Runs keep their identity and fence old executors before queue restore."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from orchestrator.permissions.models import RuntimeExecutionBinding
from orchestrator.permissions.parking import PermissionParkingStore
from orchestrator.runtime_common import QueuedRequest
from orchestrator.session_store import SessionConflict, StaleFencingToken
from tests.test_permission_pao_fence import setup_run


def setup_park(tmp_path):
    store, session, accepted, fence, context = setup_run(tmp_path)
    binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")
    item = QueuedRequest(
        request_id=accepted.request_id, chat_id=7, prompt="Write reports", source="test",
        summary="Report task", created_at=datetime.now(timezone.utc).isoformat(),
        session_id=context.session_id, run_id=context.run_id, fencing_token=fence,
        message_id=accepted.message_id, owner_id=context.owner_id, context_generation=1,
    )
    parking = PermissionParkingStore(store)
    parking.record_binding(binding)
    parking.park(binding, item, checkpoint_reference={
        "digest": "a" * 64, "her_session_id": "her-session", "turn_id": "turn",
    }, request_id="permission-request", operation_id="operation", state="waiting")
    return store, binding, item, parking


def test_park_survives_reconciliation_and_restores_same_run_without_new_message(tmp_path):
    store, binding, item, parking = setup_park(tmp_path)
    before = store.messages(binding.context.session_id, owner_id=binding.context.owner_id)
    assert not store.reconcile_incomplete_runs(agent_id="agent")
    assert store.get_run(binding.context.run_id)["state"] == "running"
    restored, token = parking.claim_resume(binding)
    assert restored.run_id == item.run_id and restored.request_id == item.request_id
    assert restored.prompt == item.prompt
    assert store.get_run(binding.context.run_id)["state"] == "queued"
    assert not store.reconcile_incomplete_runs(agent_id="agent")
    assert store.messages(binding.context.session_id, owner_id=binding.context.owner_id) == before
    with pytest.raises(StaleFencingToken):
        with store.hold_active_run(
            run_id=item.run_id, owner_id=item.owner_id, agent_id="agent", session_id=item.session_id,
            request_id=item.request_id, context_generation=1, fencing_token=binding.fencing_token,
            worker_id=binding.worker_id,
        ):
            pytest.fail("old worker retained a canonical Run claim")
    fence = store.mark_request_running(item.request_id, worker_id="INSTANCE:agent")
    successor = RuntimeExecutionBinding(replace(binding.context, execution_epoch="new-epoch"), item.request_id,
                                        fence, binding.worker_id)
    with parking.hold_resume(successor, token) as payload:
        assert payload["checkpoint"]["digest"] == "a" * 64
    with pytest.raises(SessionConflict):
        parking.claim_resume(binding)


def test_cancel_wins_over_resume_and_stopped_runs_are_not_recovered(tmp_path):
    store, binding, _, parking = setup_park(tmp_path)
    store.cancel_run(binding.context.run_id, owner_id=binding.context.owner_id, reason="user stop")
    assert not parking.list_parked(agent_id="agent")
    with pytest.raises(StaleFencingToken):
        parking.claim_resume(binding)


def test_corrupt_park_is_never_treated_as_recoverable(tmp_path):
    store, binding, _, parking = setup_park(tmp_path)
    with store._connection() as connection:
        connection.execute("UPDATE permission_run_parks SET payload_digest=?", ("b" * 64,))
    with pytest.raises(SessionConflict):
        parking.list_parked(agent_id="agent")
    with pytest.raises(SessionConflict):
        store.reconcile_incomplete_runs(agent_id="agent")
