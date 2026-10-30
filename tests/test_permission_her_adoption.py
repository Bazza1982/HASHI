"""Only a canonical parked PAO Run can adopt its exact private HER Tool batch."""
from dataclasses import replace
from datetime import datetime, timezone
import os

import pytest

from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator
from orchestrator.her_v2.session_store import HerSessionStoreError
from orchestrator.permissions.models import RuntimeExecutionBinding
from orchestrator.permissions.parking import PermissionParkingStore
from orchestrator.runtime_common import QueuedRequest
from orchestrator.session_store import StaleFencingToken
from tests.test_permission_pao_fence import setup_run


def fixture(tmp_path):
    store, session, accepted, fence, context = setup_run(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    coordinator = HerBackendSessionCoordinator(tmp_path / "her",
        execution_owner={"owner_id": "old-owner", "pid": os.getpid(), "started_at": now})
    encoded, _ = coordinator.prepare_transport(
        session_id="her-session", sections=[], resources=[], user_message="Write report",
        request_id=accepted.request_id, message_id=accepted.message_id,
        instance_id=context.instance_id, agent_id=context.agent_id, owner_id=context.owner_id,
        hashi_conversation_id=context.session_id, context_generation=1, workzone_identity="workspace",
    )
    turn = coordinator.accept(encoded)
    context = replace(context, turn_id=turn.turn_id)
    run = store.get_run(context.run_id)
    producer = dict(run_id=run["run_id"], request_id=run["request_id"], attempt=run["attempt"],
                    attempt_id=f"{run['run_id']}:attempt:{run['attempt']}",
                    state_revision=run["fencing_token"], input_digest=run["request_digest"],
                    state=run["state"], parent_run_id=run["parent_run_id"])
    checkpoint = {
        "provider": "deepseek-api", "model": "configured/model", "stage": "direct",
        "boundary": "pending_tool_batch", "producer_run_claim": producer,
        "messages": [{"role": "user", "content": "Write report"},
                     {"role": "assistant", "tool_calls": [{"id": "call", "type": "function",
                       "function": {"name": "file_write", "arguments": '{"path":"report","content":"saved"}'}}]}],
    }
    metadata = coordinator.record_provider_context_checkpoint(turn, checkpoint)
    binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")
    item = QueuedRequest(accepted.request_id, 7, "Write report", "test", "Report", now,
                         session_id=context.session_id, run_id=context.run_id, owner_id=context.owner_id,
                         message_id=accepted.message_id, fencing_token=fence)
    parking = PermissionParkingStore(store)
    parking.record_binding(binding)
    parking.park(binding, item, checkpoint_reference={
        "her_session_id": turn.session_id, "turn_id": turn.turn_id,
        "digest": metadata["digest"], "producer_run_claim": producer,
    }, request_id="access-request", operation_id="operation", state="waiting")
    restored, _ = parking.claim_resume(binding)
    next_fence = store.mark_request_running(restored.request_id, worker_id="INSTANCE:agent")
    successor = RuntimeExecutionBinding(replace(context, execution_epoch="new", worker_generation="new-worker"),
                                        accepted.request_id, next_fence, binding.worker_id)
    parking.record_binding(successor)
    return store, coordinator, turn, checkpoint, parking.resume_ticket(successor)


def test_adoption_retains_original_producer_and_accepts_only_the_explicit_successor(tmp_path):
    _, coordinator, turn, checkpoint, ticket = fixture(tmp_path)
    new_owner = {"owner_id": "new-owner", "pid": os.getpid(), "started_at": "new-generation"}
    restored = coordinator.store.adopt_permission_turn(ticket, execution_owner=new_owner)
    assert restored["messages"] == checkpoint["messages"]
    assert restored["boundary"] == "pending_tool_batch"
    with ticket.hold() as proof:
        current = proof["_current_run_claim"]
    with pytest.raises(HerSessionStoreError):
        coordinator.record_provider_context_checkpoint(turn, {**checkpoint, "producer_run_claim": current})
    successor = HerBackendSessionCoordinator(tmp_path / "her", execution_owner=new_owner)
    saved = successor.record_provider_context_checkpoint(turn, {**checkpoint, "producer_run_claim": current})
    assert saved["digest"]
    assert successor.store.provider_context_for_turn(turn.session_id, turn.turn_id)["messages"] == checkpoint["messages"]


def test_user_stop_prevents_a_parked_her_turn_from_being_reactivated(tmp_path):
    store, coordinator, _, _, ticket = fixture(tmp_path)
    store.cancel_run(ticket.binding.context.run_id, owner_id=ticket.binding.context.owner_id, reason="user stop")
    with pytest.raises(StaleFencingToken):
        coordinator.store.adopt_permission_turn(
            ticket, execution_owner={"owner_id": "new-owner", "pid": os.getpid()},
        )
