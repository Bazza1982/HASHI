"""Service-down startup keeps the actual PAO Run and accepted HER Turn."""
from dataclasses import replace
from datetime import datetime, timezone
import os

import pytest

from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator
from orchestrator.her_v2.session_store import HerSessionStoreError
from orchestrator.permissions.models import RuntimeExecutionBinding
from orchestrator.permissions.start_wait import PermissionStartStore
from orchestrator.runtime_common import QueuedRequest
from orchestrator.session_store import StaleFencingToken
from tests.test_permission_pao_fence import setup_run, run_binding


def setup_waiting(tmp_path):
    store, _, accepted, fence, context = setup_run(tmp_path)
    coordinator = HerBackendSessionCoordinator(
        tmp_path / "her", execution_owner={"owner_id": "old-her-owner", "pid": os.getpid(), "started_at": "old"},
    )
    envelope, _ = coordinator.prepare_transport(
        session_id="her-session", sections=[], resources=[], user_message="write report",
        request_id=accepted.request_id, message_id=accepted.message_id, instance_id=context.instance_id,
        agent_id=context.agent_id, owner_id=context.owner_id, hashi_conversation_id=context.session_id,
        context_generation=1, workzone_identity="workspace",
    )
    turn = coordinator.accept(envelope)
    context = replace(context, turn_id=turn.turn_id)
    claim = run_binding(context, accepted, fence)
    item = QueuedRequest(
        accepted.request_id, 7, "write report", "test", "Report", datetime.now(timezone.utc).isoformat(),
        session_id=context.session_id, run_id=context.run_id, owner_id=context.owner_id,
        message_id=accepted.message_id, fencing_token=fence, context_generation=1,
    )
    waiting = PermissionStartStore(store)
    waiting.park(claim, item, {"her_session_id": turn.session_id, "turn_id": turn.turn_id,
                               "execution_owner_id": coordinator.execution_owner["owner_id"]})
    return store, accepted, context, coordinator, turn, waiting


def resume(store, accepted, context, waiting):
    payload, = waiting.list_waiting("agent")
    item = waiting.claim_resume(payload)
    recovered, = waiting.list_waiting("agent")
    assert waiting.recover_queued(recovered).request_id == item.request_id
    fence = store.mark_request_running(accepted.request_id, worker_id="INSTANCE:agent")
    binding = RuntimeExecutionBinding(replace(context, execution_epoch="new-epoch"), accepted.request_id,
                                      fence, "INSTANCE:agent")
    return waiting.ticket(binding)


def test_service_wait_survives_reconciliation_and_adopts_only_the_original_unstarted_turn(tmp_path):
    store, accepted, context, coordinator, turn, waiting = setup_waiting(tmp_path)
    messages = store.messages(context.session_id, owner_id=context.owner_id)
    assert not store.reconcile_incomplete_runs(agent_id=context.agent_id)
    assert store.get_run(context.run_id)["state"] == "running"
    ticket = resume(store, accepted, context, waiting)
    with pytest.raises(StaleFencingToken):
        with store.hold_active_run(**run_binding(context, accepted, 1)):
            pytest.fail("the old attempt must remain fenced")
    new_owner = {"owner_id": "new-her-owner", "pid": os.getpid(), "started_at": "new"}
    coordinator.store.adopt_unstarted_permission_turn(ticket, execution_owner=new_owner)
    ticket.finish()
    assert not waiting.list_waiting("agent")
    assert store.messages(context.session_id, owner_id=context.owner_id) == messages
    assert coordinator.store.provider_context_for_turn(turn.session_id, turn.turn_id) is None
    assert not (tmp_path / "permissions.sqlite").exists()



@pytest.mark.asyncio
async def test_configure_service_outage_never_enters_the_her_execution_loop(tmp_path):
    from types import SimpleNamespace
    from orchestrator.her_v2.config import HERv2Config
    from orchestrator.permissions.clients import RemoteRunExecutor
    from orchestrator.permissions.continuation import PermissionPark

    _, _, accepted, fence, context = setup_run(tmp_path)
    binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")
    class Client:
        async def call_async(self, method, arguments):
            assert method == "configure_inference"
            raise OSError("the service connection disappeared")
    class Runtime:
        config = HERv2Config.from_mapping({"main": {"provider": "deepseek", "model": "deepseek-v4-pro"}})
        entered = False
        async def run_turn(self, *args, **kwargs):
            self.entered = True
            pytest.fail("no inference or Tool loop may start without its frozen model capability")
    executor = RemoteRunExecutor.__new__(RemoteRunExecutor)
    executor.binding = binding
    executor._owner = SimpleNamespace(_client=Client())
    executor._execution_client = SimpleNamespace()
    executor._level = "L1"
    executor._permission_prestart_failure = False
    runtime = Runtime()
    with pytest.raises(PermissionPark) as parked:
        await executor.run_her(runtime, "write report", accepted.request_id, binding=binding)
    assert parked.value.result.state == "unavailable"
    assert executor._permission_prestart_failure
    assert not runtime.entered


def test_user_stop_wins_over_waiting_service_recovery(tmp_path):
    store, _, context, _, _, waiting = setup_waiting(tmp_path)
    payload, = waiting.list_waiting("agent")
    store.cancel_run(context.run_id, owner_id=context.owner_id, reason="user stop")
    assert not waiting.list_waiting("agent")
    with pytest.raises(StaleFencingToken):
        waiting.claim_resume(payload)


def test_unstarted_adoption_cannot_discard_an_existing_provider_boundary(tmp_path):
    store, accepted, context, coordinator, turn, waiting = setup_waiting(tmp_path)
    coordinator.record_provider_context_checkpoint(turn, {
        "provider": "deepseek-api", "model": "configured/model", "stage": "direct",
        "boundary": "ready_provider_request", "messages": [{"role": "user", "content": "write report"}],
    })
    ticket = resume(store, accepted, context, waiting)
    with pytest.raises(HerSessionStoreError, match="no longer unstarted"):
        coordinator.store.adopt_unstarted_permission_turn(
            ticket, execution_owner={"owner_id": "new-owner", "pid": os.getpid()},
        )
