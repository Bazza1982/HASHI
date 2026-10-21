from __future__ import annotations

import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from adapters.her_v2 import HERv2Adapter
from adapters.her_v2_provider import HashiStageProvider
from orchestrator.her_v2.audit import DurableAuditLog
from orchestrator.her_v2.backend_session import (
    HerBackendSessionCoordinator,
    HerFixedProtocolError,
)
from orchestrator.her_v2.ledger import ExecutionLedger, LedgerStore
from orchestrator.her_v2.models import LifecycleState
from orchestrator.her_v2.session_store import HerSessionStore, HerSessionStoreError


def _sections():
    return [
        {
            "key": "permanent_system",
            "title": "SYSTEM",
            "text": "Stable policy",
            "authority": "permanent_system",
            "order": 0,
        },
        {
            "key": "persona",
            "title": "PERSONA",
            "text": "Friendly",
            "authority": "persona",
            "order": 1,
        },
    ]


def _accept(
    coordinator: HerBackendSessionCoordinator,
    request_id: str,
    message: str,
):
    encoded, _audit = coordinator.prepare_transport(
        session_id="session-1",
        sections=_sections(),
        resources=[],
        user_message=message,
        request_id=request_id,
        message_id=f"message-{request_id}",
        instance_id="HASHI3",
        agent_id="agent1",
        owner_id="owner",
        hashi_conversation_id="conversation",
        context_generation=1,
        workzone_identity="workzone",
    )
    return coordinator.accept(encoded)


def _route(provider: str, model: str):
    return {
        "routing_mode": "single",
        "profiles": [
            {
                "name": "premium",
                "provider": provider,
                "model": model,
                "reasoning": "high",
            }
        ],
    }


def _runtime_event(turn_id: str, event_id: str, event: str, **payload):
    return {
        "event_id": event_id,
        "turn_id": turn_id,
        "request_ref": f"hashi-request:{turn_id}",
        "stage": payload.pop("stage", "execution"),
        "role": "primary",
        "event": event,
        "provider": "deepseek-api",
        "model": "deepseek-v4-pro",
        "attempt": 1,
        "payload": payload,
    }


def _run_claim(
    run_id: str,
    request_id: str,
    *,
    attempt: int = 1,
    state_revision: int = 1,
    input_digest: str = "sha256:input",
    state: str = "running",
    parent_run_id: str | None = None,
):
    return {
        "run_id": run_id,
        "request_id": request_id,
        "attempt": attempt,
        "attempt_id": f"{run_id}:attempt:{attempt}",
        "state_revision": state_revision,
        "input_digest": input_digest,
        "state": state,
        "parent_run_id": parent_run_id,
    }


def test_session_store_closes_database_handles_after_success_and_failure(tmp_path, monkeypatch):
    from orchestrator.her_v2 import session_store

    opened = []
    connect = sqlite3.connect
    def tracked_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        opened.append(connection)
        return connection
    monkeypatch.setattr(session_store.sqlite3, "connect", tracked_connect)
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    turn = _accept(coordinator, "turn-1", "Keep the durable state")
    assert coordinator.store.session(turn.session_id)
    with pytest.raises(RuntimeError, match="interrupted"):
        with coordinator.store._transaction() as connection:
            connection.execute("DELETE FROM her_sessions")
            raise RuntimeError("interrupted")
    assert coordinator.store.session(turn.session_id)
    assert opened
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    coordinator.store.path.rename(tmp_path / "closed.sqlite3")


def test_route_revision_rebuilds_from_settled_checkpoint_without_new_session(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    first = _accept(coordinator, "turn-1", "Build it")
    frozen = coordinator.store.freeze_turn_routing(
        session_id=first.session_id,
        turn_id=first.turn_id,
        routing_revision=1,
        capability_revision=1,
        pricing_revision="prices-v1",
        route_snapshot=_route("deepseek-api", "deepseek-v4-pro"),
    )
    assert frozen["rebuild_from_checkpoint"] is False
    coordinator.store.record_runtime_event(
        session_id=first.session_id,
        turn_id=first.turn_id,
        record=_runtime_event(
            first.turn_id,
            "turn-1:strategy",
            "strategy_recorded",
            stage="triage",
            selected_strategy_cards=["CODE_MODIFY"],
            execution_brief={"stages": ["inspect", "change", "verify"]},
        ),
    )
    coordinator.store.record_runtime_event(
        session_id=first.session_id,
        turn_id=first.turn_id,
        record=_runtime_event(
            first.turn_id,
            "turn-1:plan",
            "stage_completed",
            stage="planning",
            output={"steps": ["inspect", "change", "verify"]},
        ),
    )
    coordinator.complete(first, assistant_text="Done")

    checkpoint = coordinator.store.settled_checkpoint(first.session_id)
    assert checkpoint is not None
    assert checkpoint["payload"]["strategy"]["selected_strategy_cards"] == [
        "CODE_MODIFY"
    ]

    second = _accept(coordinator, "turn-2", "Continue")
    rebuilt = coordinator.store.freeze_turn_routing(
        session_id=second.session_id,
        turn_id=second.turn_id,
        routing_revision=2,
        capability_revision=1,
        pricing_revision="prices-v1",
        route_snapshot=_route("hashi-api", "gpt-5.6-sol"),
    )

    assert second.session_id == first.session_id
    assert rebuilt["rebuild_from_checkpoint"] is True
    assert rebuilt["provider_context_generation"] == 2
    assert rebuilt["checkpoint"]["settled_through_turn_id"] == "turn-1"


def test_unsettled_route_attempt_does_not_replace_last_settled_route(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    first = _accept(coordinator, "turn-1", "Start on provider A")
    coordinator.store.freeze_turn_routing(
        session_id=first.session_id,
        turn_id=first.turn_id,
        routing_revision=1,
        capability_revision=1,
        pricing_revision="prices-v1",
        route_snapshot=_route("deepseek-api", "deepseek-v4-pro"),
    )
    coordinator.complete(first, assistant_text="Settled on A")

    second = _accept(coordinator, "turn-2", "Switch to provider B")
    attempted = coordinator.store.freeze_turn_routing(
        session_id=second.session_id,
        turn_id=second.turn_id,
        routing_revision=2,
        capability_revision=1,
        pricing_revision="prices-v1",
        route_snapshot=_route("hashi-api", "gpt-5.6-sol"),
    )
    assert attempted["rebuild_from_checkpoint"] is True
    assert attempted["provider_context_generation"] == 2
    assert coordinator.store.reconcile_interrupted() == 1

    third = _accept(coordinator, "turn-3", "Recover on provider B")
    rebuilt_again = coordinator.store.freeze_turn_routing(
        session_id=third.session_id,
        turn_id=third.turn_id,
        routing_revision=2,
        capability_revision=1,
        pricing_revision="prices-v1",
        route_snapshot=_route("hashi-api", "gpt-5.6-sol"),
    )

    assert rebuilt_again["rebuild_from_checkpoint"] is True
    assert rebuilt_again["provider_context_generation"] == 3
    assert rebuilt_again["checkpoint"]["settled_through_turn_id"] == "turn-1"


def test_freeze_turn_routing_replay_preserves_original_rebuild_decision(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    first = _accept(coordinator, "turn-1", "Start")
    coordinator.store.freeze_turn_routing(
        session_id=first.session_id,
        turn_id=first.turn_id,
        routing_revision=1,
        capability_revision=1,
        pricing_revision="prices-v1",
        route_snapshot=_route("deepseek-api", "deepseek-v4-pro"),
    )
    coordinator.complete(first, assistant_text="Settled")
    second = _accept(coordinator, "turn-2", "Switch")
    kwargs = {
        "session_id": second.session_id,
        "turn_id": second.turn_id,
        "routing_revision": 2,
        "capability_revision": 1,
        "pricing_revision": "prices-v1",
        "route_snapshot": _route("hashi-api", "gpt-5.6-sol"),
    }

    original = coordinator.store.freeze_turn_routing(**kwargs)
    replay = coordinator.store.freeze_turn_routing(**kwargs)

    assert original["rebuild_from_checkpoint"] is True
    assert replay["rebuild_from_checkpoint"] is True
    assert replay["canonical_sequence"] == original["canonical_sequence"]


def test_interrupted_unreceipted_side_effect_is_truthfully_unknown(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    turn = _accept(coordinator, "turn-1", "Change production")
    coordinator.store.freeze_turn_routing(
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        routing_revision=1,
        capability_revision=1,
        pricing_revision="prices-v1",
        route_snapshot=_route("deepseek-api", "deepseek-v4-pro"),
    )
    coordinator.store.record_runtime_event(
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        record=_runtime_event(
            turn.turn_id,
            "turn-1:tool:deploy:intent",
            "tool_intent",
            tool_call_id="deploy",
            tool_name="deployment_apply",
            arguments_sha256="sha256:abc",
            read_only=False,
        ),
    )

    assert coordinator.store.reconcile_interrupted() == 1
    recovery = coordinator.store.active_turn_recovery(turn.session_id, turn.turn_id)
    assert recovery is not None
    assert recovery["status"] == "terminated"
    assert recovery["safe_to_resume"] is False
    assert recovery["recovery_disposition"] == "UNKNOWN_SIDE_EFFECT"

    next_turn = _accept(coordinator, "turn-2", "Recover safely")
    context = coordinator.store.consume_recovery_context(
        session_id=next_turn.session_id,
        current_turn_id=next_turn.turn_id,
    )
    assert context is not None
    assert context["recovery_disposition"] == "UNKNOWN_SIDE_EFFECT"
    assert context["side_effects"][0]["state"] == "pending"

    # Consumption is a durable handoff, not premature settlement.  A second
    # crash must advance the chain while retaining the original unknown effect.
    original = coordinator.store.active_turn_recovery(
        turn.session_id, turn.turn_id
    )
    handed_off = coordinator.store.active_turn_recovery(
        next_turn.session_id, next_turn.turn_id
    )
    assert original["status"] == "terminated"
    assert handed_off["recovery_source_turn_id"] == turn.turn_id
    assert handed_off["side_effects"][0]["tool_call_id"] == "deploy"

    assert coordinator.store.reconcile_interrupted() == 1
    third_turn = _accept(coordinator, "turn-3", "Continue the recovery")
    chained = coordinator.store.consume_recovery_context(
        session_id=third_turn.session_id,
        current_turn_id=third_turn.turn_id,
    )
    assert chained["interrupted_turn_id"] == next_turn.turn_id
    assert chained["side_effects"][0]["tool_call_id"] == "deploy"

    coordinator.complete(third_turn, assistant_text="Recovery was investigated.")
    assert coordinator.store.active_turn_recovery(
        turn.session_id, turn.turn_id
    )["status"] == "archived"
    assert coordinator.store.active_turn_recovery(
        next_turn.session_id, next_turn.turn_id
    )["status"] == "archived"


def test_steered_turn_is_recoverable_once_while_cancelled_turn_is_not(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    steered = _accept(coordinator, "turn-steered", "Do the original work")
    coordinator.store.record_runtime_event(
        session_id=steered.session_id,
        turn_id=steered.turn_id,
        record=_runtime_event(
            steered.turn_id,
            "turn-steered:tool:read:receipt",
            "tool_receipt",
            tool_call_id="read",
            receipt={
                "tool_call_id": "read",
                "tool_name": "file_read",
                "status": "success",
                "read_only": True,
                "completed": True,
                "output_sha256": "sha256:read",
            },
        ),
    )
    provider_messages = [
        {"role": "system", "content": "private system context"},
        {"role": "user", "content": "Do the original work"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "Inspect the file before changing it.",
            "tool_calls": [
                {
                    "id": "read",
                    "type": "function",
                    "function": {
                        "name": "file_read",
                        "arguments": '{"path":"target.txt"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "read", "content": "old content"},
    ]
    checkpoint = coordinator.record_provider_context_checkpoint(
        steered,
        {
            "provider": "deepseek-api",
            "model": "deepseek-reasoner",
            "effort": "high",
            "stage": "execution",
            "boundary": "completed_tool_batch",
            "messages": provider_messages,
            "reasoning": [
                {
                    "state": "available",
                    "content": "Inspect the file before changing it.",
                }
            ],
            "provider_request_id": "provider-read",
            "invocation_id": "turn-steered:execution:1",
        },
    )
    assert checkpoint["available"] is True
    assert checkpoint["reasoning_count"] == 1

    closed = coordinator.steer(
        steered,
        reason="The user supplied a new direction.",
    )

    assert closed["status"] == "steered"
    recovery = coordinator.store.active_turn_recovery(
        steered.session_id, steered.turn_id
    )
    assert recovery["status"] == "terminated"
    assert recovery["recovery_disposition"] == "steered"
    source_claim = _run_claim(
        "run-steered",
        "turn-steered",
        state="superseded",
        input_digest="sha256:steered-input",
    )
    evidence = coordinator.store.interruption_evidence_for_request(
        "turn-steered",
        run_claim=source_claim,
    )
    assert evidence["evidence_ref"].endswith(f":{steered.turn_id}")
    assert evidence["recovery_disposition"] == "steered"
    assert evidence["tool_receipt_count"] == 1
    assert evidence["provider_context"]["available"] is True
    assert evidence["provider_context"]["reasoning_count"] == 1
    assert evidence["run_id"] == "run-steered"
    assert evidence["attempt_id"] == "run-steered:attempt:1"
    assert evidence["state_revision"] == 1
    assert evidence["input_digest"] == "sha256:steered-input"
    assert evidence["provider"] == "deepseek-api"
    assert evidence["model"] == "deepseek-reasoner"
    assert evidence["effort"] == "high"
    assert evidence["evidence_digest"].startswith("sha256:")

    successor_claim = _run_claim(
        "run-successor",
        "turn-successor",
        state="running",
        parent_run_id="run-steered",
        input_digest="sha256:successor-input",
    )
    binding = {
        "instance_id": "HASHI3",
        "agent_id": "agent1",
        "owner_id": "owner",
        "hashi_conversation_id": "conversation",
        "context_generation": 1,
        "workzone_identity": "workzone",
    }
    assert coordinator.store.verified_steer_session_id(
        evidence,
        source_run_claim=source_claim,
        successor_run_claim=successor_claim,
        **binding,
    ) == steered.session_id

    # Reopening the durable store under the same exact PAO attempts remains
    # valid; a newer source attempt makes the old checkpoint diagnostic-only.
    reopened = HerSessionStore(coordinator.store.path)
    assert reopened.verified_steer_session_id(
        evidence,
        source_run_claim=source_claim,
        successor_run_claim=successor_claim,
        **binding,
    ) == steered.session_id
    with sqlite3.connect(reopened.path) as connection:
        event_count_before = connection.execute(
            "SELECT count(*) FROM her_session_events WHERE session_id = ?",
            (steered.session_id,),
        ).fetchone()[0]
    stale_source_claim = {
        **source_claim,
        "attempt": 2,
        "attempt_id": "run-steered:attempt:2",
        "state_revision": 2,
    }
    assert reopened.verified_steer_session_id(
        evidence,
        source_run_claim=stale_source_claim,
        successor_run_claim=successor_claim,
        **binding,
    ) is None
    with sqlite3.connect(reopened.path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM her_session_events WHERE session_id = ?",
            (steered.session_id,),
        ).fetchone()[0] == event_count_before
    assert reopened.verified_steer_session_id(
        evidence,
        source_run_claim=source_claim,
        successor_run_claim={**successor_claim, "state": "completed"},
        **binding,
    ) is None
    assert reopened.verified_steer_session_id(
        {**evidence, "effort": "low"},
        source_run_claim=source_claim,
        successor_run_claim=successor_claim,
        **binding,
    ) is None

    successor = _accept(coordinator, "turn-successor", "Continue differently")
    context = coordinator.store.consume_recovery_context(
        session_id=successor.session_id,
        current_turn_id=successor.turn_id,
    )
    assert context["interrupted_turn_id"] == steered.turn_id
    assert context["recovery_disposition"] == "steered"
    assert context["tool_receipts"][0]["tool_call_id"] == "read"
    assert context["provider_context_checkpoint"]["available"] is True
    assert "Inspect the file" not in json.dumps(context)
    private_context = coordinator.store.provider_context_for_turn(
        successor.session_id,
        successor.turn_id,
    )
    assert private_context["messages"] == provider_messages
    assert private_context["reasoning"][0]["content"].startswith("Inspect")

    coordinator.complete(successor, assistant_text="Done")
    cancelled = _accept(coordinator, "turn-cancelled", "Stop this")
    coordinator.cancel(cancelled, reason="The user stopped the task.")
    next_turn = _accept(coordinator, "turn-after-cancel", "A separate task")
    assert coordinator.store.consume_recovery_context(
        session_id=next_turn.session_id,
        current_turn_id=next_turn.turn_id,
    ) is None


def test_provider_context_checkpoint_is_segmented_without_count_or_size_ceiling(
    tmp_path,
):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    turn = _accept(coordinator, "turn-long-context", "Keep working for as long as needed")
    large_result = "evidence:" + ("x" * 33_000)
    messages = []
    for index in range(260):
        call_id = f"call-{index}"
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "file_read", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": large_result,
                },
            ]
        )

    checkpoint = coordinator.record_provider_context_checkpoint(
        turn,
        {
            "provider": "deepseek-api",
            "model": "deepseek-flash",
            "stage": "execution",
            "boundary": "completed_tool_batch",
            "messages": messages,
            "provider_request_id": "provider-long-context",
            "invocation_id": "turn-long-context:execution:1",
        },
    )

    assert checkpoint["message_count"] == 520
    restored = coordinator.store.provider_context_for_turn(
        turn.session_id, turn.turn_id
    )
    assert restored["messages"] == messages
    with sqlite3.connect(coordinator.store.path) as connection:
        metadata_size = connection.execute(
            """
            SELECT length(provider_context_json)
            FROM her_active_turn_recovery
            WHERE session_id = ? AND turn_id = ?
            """,
            (turn.session_id, turn.turn_id),
        ).fetchone()[0]
        item_count = connection.execute(
            """
            SELECT count(*) FROM her_provider_context_items
            WHERE session_id = ? AND turn_id = ? AND item_kind = 'message'
            """,
            (turn.session_id, turn.turn_id),
        ).fetchone()[0]
    assert metadata_size < 2_000
    assert item_count == len(messages)


def test_active_recovery_retains_every_completed_tool_receipt(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    turn = _accept(coordinator, "turn-many-tools", "Perform a long investigation")

    for index in range(300):
        call_id = f"read-{index}"
        coordinator.store.record_runtime_event(
            session_id=turn.session_id,
            turn_id=turn.turn_id,
            record=_runtime_event(
                turn.turn_id,
                f"{turn.turn_id}:tool:{call_id}:receipt",
                "tool_receipt",
                tool_call_id=call_id,
                receipt={
                    "tool_call_id": call_id,
                    "tool_name": "file_read",
                    "status": "success",
                    "read_only": True,
                    "completed": True,
                    "output_sha256": f"sha256:{index:064x}",
                },
            ),
        )

    recovery = coordinator.store.active_turn_recovery(
        turn.session_id, turn.turn_id
    )
    assert len(recovery["tool_receipts"]) == 300
    assert recovery["tool_receipts"][0]["tool_call_id"] == "read-0"
    assert recovery["tool_receipts"][-1]["tool_call_id"] == "read-299"


def test_candidate_worker_does_not_reconcile_a_live_owner_turn(tmp_path):
    root = tmp_path / "state"
    production = HerBackendSessionCoordinator(
        root,
        execution_owner={
            "owner_id": "worker-production",
            "pid": os.getpid(),
            "started_at": "2026-09-14T12:00:00+00:00",
        },
    )
    turn = _accept(production, "turn-live", "Keep working")
    candidate = HerBackendSessionCoordinator(
        root,
        execution_owner={
            "owner_id": "worker-candidate",
            "pid": os.getpid(),
            "started_at": "2026-09-14T12:01:00+00:00",
        },
    )

    assert candidate.reconcile_interrupted() == 0
    with sqlite3.connect(production.store.path) as connection:
        status = connection.execute(
            "SELECT status FROM her_turns WHERE session_id = ? AND turn_id = ?",
            (turn.session_id, turn.turn_id),
        ).fetchone()[0]
    assert status == "active"


def test_candidate_reconciles_only_after_the_recorded_owner_is_dead(tmp_path):
    root = tmp_path / "state"
    production = HerBackendSessionCoordinator(
        root,
        execution_owner={
            "owner_id": "worker-dead",
            "pid": 404404,
            "started_at": "2026-09-14T12:00:00+00:00",
        },
    )
    turn = _accept(production, "turn-dead", "Recover after a crash")
    candidate = HerBackendSessionCoordinator(
        root,
        execution_owner={
            "owner_id": "worker-takeover",
            "pid": os.getpid(),
            "started_at": "2026-09-14T12:01:00+00:00",
        },
    )

    reconciled = candidate.reconcile_interrupted_turns(
        process_is_alive=lambda _pid: False
    )
    assert [row["request_id"] for row in reconciled] == ["turn-dead"]
    recovery = candidate.store.active_turn_recovery(
        turn.session_id,
        turn.turn_id,
    )
    assert recovery["status"] == "terminated"
    assert recovery["recovery_disposition"] == "FAILED_SAFE_REPLAY_REQUIRED"


def test_different_execution_owner_cannot_close_an_active_turn(tmp_path):
    root = tmp_path / "state"
    production = HerBackendSessionCoordinator(
        root,
        execution_owner={
            "owner_id": "worker-production",
            "pid": os.getpid(),
            "started_at": "2026-09-14T12:00:00+00:00",
        },
    )
    turn = _accept(production, "turn-owned", "Keep ownership")
    candidate = HerBackendSessionCoordinator(
        root,
        execution_owner={
            "owner_id": "worker-candidate",
            "pid": os.getpid(),
            "started_at": "2026-09-14T12:01:00+00:00",
        },
    )

    with pytest.raises(HerFixedProtocolError) as caught:
        candidate.complete(turn, assistant_text="Wrong owner")
    assert caught.value.code == "execution_owner_conflict"
    with pytest.raises(HerFixedProtocolError) as caught:
        candidate.cancel(turn, reason="Wrong owner")
    assert caught.value.code == "execution_owner_conflict"


@pytest.mark.parametrize(
    ("receipt_status", "effect_state"),
    [("success", "completed"), ("failed", "failed")],
)
def test_late_completed_receipt_repairs_same_owner_false_reconciliation(
    tmp_path,
    receipt_status,
    effect_state,
):
    coordinator = HerBackendSessionCoordinator(
        tmp_path / "state",
        execution_owner={
            "owner_id": "worker-production",
            "pid": os.getpid(),
            "started_at": "2026-09-14T12:00:00+00:00",
        },
    )
    turn = _accept(coordinator, "turn-late", "Wait for the command")
    coordinator.record_runtime_event(
        turn,
        _runtime_event(
            turn.turn_id,
            "turn-late:tool:wait:intent",
            "tool_intent",
            operation_id="turn-late:wait:1",
            tool_call_id="wait",
            tool_name="shell",
            arguments_sha256="sha256:wait",
            read_only=False,
        ),
    )

    # Simulate the pre-fix candidate behavior.  The original owner is still
    # executing and later records the terminal receipt on the same sequence.
    assert coordinator.store.reconcile_interrupted() == 1
    assert (
        coordinator.store.active_turn_recovery(
            turn.session_id,
            turn.turn_id,
        )["recovery_disposition"]
        == "UNKNOWN_SIDE_EFFECT"
    )

    coordinator.record_runtime_event(
        turn,
        _runtime_event(
            turn.turn_id,
            "turn-late:tool:wait:receipt",
            "tool_receipt",
            operation_id="turn-late:wait:1",
            tool_call_id="wait",
            receipt={
                "operation_id": "turn-late:wait:1",
                "tool_call_id": "wait",
                "tool_name": "shell",
                "status": receipt_status,
                "completed": True,
                "exit_code": 0,
            },
        ),
    )
    recovery = coordinator.store.active_turn_recovery(
        turn.session_id,
        turn.turn_id,
    )
    assert recovery["safe_to_resume"] is True
    assert recovery["recovery_disposition"] == ""
    assert recovery["side_effects"][0]["state"] == effect_state
    assert recovery["remaining_work"]["unresolved_side_effects"] == []
    assert recovery["remaining_work"]["status"] == "in_progress"

    closed = coordinator.complete(turn, assistant_text="The command completed.")
    assert closed["status"] == "completed"
    assert closed["assistant_text"] == "The command completed."


def test_shadow_ledger_reconciliation_is_scoped_to_confirmed_dead_requests(
    tmp_path,
):
    store = LedgerStore(tmp_path / "ledgers")
    live = ExecutionLedger(
        "ledger-live",
        "hashi-request:request-live",
        "sha256:live",
    )
    dead = ExecutionLedger(
        "ledger-dead",
        "hashi-request:request-dead",
        "sha256:dead",
    )
    store.save(live)
    store.save(dead)

    reconciled = store.reconcile_interrupted(
        request_refs={"hashi-request:request-dead"}
    )

    assert [ledger.turn_id for ledger in reconciled] == ["ledger-dead"]
    assert store.load("ledger-live").status is LifecycleState.RECEIVED
    assert store.load("ledger-dead").status is LifecycleState.ERROR


def test_runtime_event_projection_is_idempotent(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    turn = _accept(coordinator, "turn-1", "Read")
    record = _runtime_event(
        turn.turn_id,
        "turn-1:tool:read:receipt",
        "tool_receipt",
        tool_call_id="read",
        receipt={
            "tool_call_id": "read",
            "tool_name": "file_read",
            "status": "success",
            "read_only": True,
            "completed": True,
            "output_sha256": "sha256:def",
        },
    )
    first_sequence = coordinator.store.record_runtime_event(
        session_id=turn.session_id, turn_id=turn.turn_id, record=record
    )
    second_sequence = coordinator.store.record_runtime_event(
        session_id=turn.session_id, turn_id=turn.turn_id, record=record
    )
    recovery = coordinator.store.active_turn_recovery(turn.session_id, turn.turn_id)
    assert first_sequence == second_sequence
    assert len(recovery["tool_receipts"]) == 1

    conflicting = _runtime_event(
        turn.turn_id,
        "turn-1:tool:read:receipt",
        "tool_receipt",
        tool_call_id="read",
        receipt={
            "tool_call_id": "read",
            "tool_name": "file_read",
            "status": "failed",
            "read_only": True,
            "completed": False,
            "output_sha256": "sha256:changed",
        },
    )
    with pytest.raises(HerSessionStoreError) as exc_info:
        coordinator.store.record_runtime_event(
            session_id=turn.session_id,
            turn_id=turn.turn_id,
            record=conflicting,
        )
    assert exc_info.value.code == "event_id_conflict"

    comparison = coordinator.store.compare_wip_shadow(
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        wip_event_ids=[record["event_id"], "shadow-only-extra"],
    )
    assert comparison["missing_from_wip_count"] == 0
    assert comparison["extra_in_wip"] == ["shadow-only-extra"]
    assert comparison["parity"] is False


def test_completed_failed_side_effect_receipt_is_durable_not_inflight(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    turn = _accept(coordinator, "turn-1", "Apply a change")
    coordinator.store.record_runtime_event(
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        record=_runtime_event(
            turn.turn_id,
            "turn-1:tool:write:intent",
            "tool_intent",
            tool_call_id="write",
            tool_name="hashi_file_write",
            arguments_sha256="sha256:write",
            read_only=False,
        ),
    )
    coordinator.store.record_runtime_event(
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        record=_runtime_event(
            turn.turn_id,
            "turn-1:tool:write:receipt",
            "tool_receipt",
            tool_call_id="write",
            receipt={
                "tool_call_id": "write",
                "tool_name": "hashi_file_write",
                "status": "failed",
                "read_only": False,
                "completed": True,
                "output_sha256": "sha256:partial",
            },
        ),
    )

    recovery = coordinator.store.active_turn_recovery(turn.session_id, turn.turn_id)
    assert recovery["side_effects"][0]["state"] == "failed"
    assert recovery["side_effects"][0]["receipt_status"] == "failed"
    assert recovery["safe_to_resume"] is True
    closed = coordinator.complete(turn, assistant_text="Recovered and verified")
    assert closed["status"] == "completed"
    assert coordinator.store.settled_checkpoint(turn.session_id) is not None


def test_incomplete_failed_side_effect_receipt_remains_truthfully_unknown(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    turn = _accept(coordinator, "turn-1", "Apply a change")
    coordinator.store.record_runtime_event(
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        record=_runtime_event(
            turn.turn_id,
            "turn-1:tool:write:intent",
            "tool_intent",
            tool_call_id="write",
            tool_name="hashi_file_write",
            arguments_sha256="sha256:write",
            read_only=False,
        ),
    )
    coordinator.store.record_runtime_event(
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        record=_runtime_event(
            turn.turn_id,
            "turn-1:tool:write:receipt",
            "tool_receipt",
            tool_call_id="write",
            receipt={
                "tool_call_id": "write",
                "tool_name": "hashi_file_write",
                "status": "failed",
                "read_only": False,
                "completed": False,
                "output_sha256": "sha256:partial",
            },
        ),
    )

    recovery = coordinator.store.active_turn_recovery(turn.session_id, turn.turn_id)
    assert recovery["side_effects"][0]["state"] == "unknown"
    assert recovery["safe_to_resume"] is False
    assert coordinator.store.reconcile_interrupted() == 1
    assert coordinator.store.active_turn_recovery(
        turn.session_id, turn.turn_id
    )["recovery_disposition"] == "UNKNOWN_SIDE_EFFECT"


def test_receipt_only_settles_its_exact_tool_operation(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    turn = _accept(coordinator, "turn-1", "Apply two changes")
    for invocation in ("execution:invocation:1", "execution:invocation:2"):
        coordinator.store.record_runtime_event(
            session_id=turn.session_id,
            turn_id=turn.turn_id,
            record=_runtime_event(
                turn.turn_id,
                f"{invocation}:intent",
                "tool_intent",
                operation_id=f"{invocation}:attempt:1:tool:call-1",
                invocation_id=invocation,
                attempt=1,
                tool_call_id="call-1",
                tool_name="hashi_file_write",
                arguments_sha256=f"sha256:{invocation}",
                read_only=False,
            ),
        )
    coordinator.store.record_runtime_event(
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        record=_runtime_event(
            turn.turn_id,
            "execution:invocation:2:receipt",
            "tool_receipt",
            operation_id="execution:invocation:2:attempt:1:tool:call-1",
            tool_call_id="call-1",
            receipt={
                "tool_call_id": "call-1",
                "tool_name": "hashi_file_write",
                "status": "success",
                "read_only": False,
                "completed": True,
                "output_sha256": "sha256:second",
            },
        ),
    )

    recovery = coordinator.store.active_turn_recovery(turn.session_id, turn.turn_id)
    assert [effect["state"] for effect in recovery["side_effects"]] == [
        "pending",
        "completed",
    ]
    assert recovery["safe_to_resume"] is False


def test_success_cannot_settle_an_unresolved_side_effect(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    turn = _accept(coordinator, "turn-1", "Apply a change")
    coordinator.store.record_runtime_event(
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        record=_runtime_event(
            turn.turn_id,
            "execution:write:intent",
            "tool_intent",
            operation_id="execution:1:attempt:1:tool:write",
            invocation_id="execution:1",
            attempt=1,
            tool_call_id="write",
            tool_name="hashi_file_write",
            arguments_sha256="sha256:write",
            read_only=False,
        ),
    )

    closed = coordinator.complete(turn, assistant_text="Reported done")

    assert closed["status"] == "failed"
    assert coordinator.store.settled_checkpoint(turn.session_id) is None
    recovery = coordinator.store.active_turn_recovery(turn.session_id, turn.turn_id)
    assert recovery["recovery_disposition"] == "UNKNOWN_SIDE_EFFECT"
    assert recovery["safe_to_resume"] is False


def test_cancelled_unknown_side_effect_remains_recoverable(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    turn = _accept(coordinator, "turn-1", "Deploy")
    coordinator.store.record_runtime_event(
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        record=_runtime_event(
            turn.turn_id,
            "execution:deploy:intent",
            "tool_intent",
            operation_id="execution:1:attempt:1:tool:deploy",
            invocation_id="execution:1",
            attempt=1,
            tool_call_id="deploy",
            tool_name="deployment_apply",
            arguments_sha256="sha256:deploy",
            read_only=False,
        ),
    )

    closed = coordinator.cancel(turn, reason="User stopped")
    assert closed["status"] == "cancelled"
    recovery = coordinator.store.active_turn_recovery(turn.session_id, turn.turn_id)
    assert recovery["recovery_disposition"] == "UNKNOWN_SIDE_EFFECT"

    next_turn = _accept(coordinator, "turn-2", "Investigate before retry")
    context = coordinator.store.consume_recovery_context(
        session_id=next_turn.session_id,
        current_turn_id=next_turn.turn_id,
    )
    assert context["recovery_disposition"] == "UNKNOWN_SIDE_EFFECT"
    assert context["side_effects"][0]["state"] == "pending"


def test_recovery_projection_never_truncates_unresolved_side_effects():
    effects = [
        {"tool_call_id": f"resolved-{index}", "state": "completed"}
        for index in range(300)
    ]
    effects.insert(0, {"tool_call_id": "oldest-pending", "state": "pending"})
    effects.insert(1, {"tool_call_id": "oldest-unknown", "state": "unknown"})

    retained = HerSessionStore._recovery_side_effects(effects)

    assert len(retained) == 302
    assert {row["tool_call_id"] for row in retained} >= {
        "oldest-pending",
        "oldest-unknown",
    }


def test_recovery_projection_never_truncates_unresolved_receipts():
    receipts = [
        {
            "operation_id": f"operation-{index}",
            "tool_call_id": f"call-{index}",
            "status": "success",
            "completed": True,
        }
        for index in range(300)
    ]
    receipts.insert(
        0,
        {
            "operation_id": "oldest-unknown-operation",
            "tool_call_id": "oldest-unknown-call",
            "status": "failed",
            "completed": True,
        },
    )
    effects = [
        {
            "operation_id": "oldest-unknown-operation",
            "tool_call_id": "oldest-unknown-call",
            "state": "unknown",
        }
    ]

    retained = HerSessionStore._recovery_tool_receipts(receipts, effects)

    assert len(retained) == 301
    assert retained[0]["operation_id"] == "oldest-unknown-operation"
    assert retained[-1]["operation_id"] == "operation-299"


def test_legacy_active_turn_without_projection_fails_closed(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    turn = _accept(coordinator, "turn-1", "Legacy active work")
    with sqlite3.connect(coordinator.store.path) as connection:
        connection.execute(
            """
            DELETE FROM her_active_turn_recovery
            WHERE session_id = ? AND turn_id = ?
            """,
            (turn.session_id, turn.turn_id),
        )

    assert coordinator.store.reconcile_interrupted() == 1
    recovery = coordinator.store.active_turn_recovery(turn.session_id, turn.turn_id)

    assert recovery["recovery_disposition"] == "UNKNOWN_SIDE_EFFECT"
    assert recovery["safe_to_resume"] is False
    assert recovery["side_effects"][0]["tool_name"] == (
        "legacy_wip_recovery_boundary"
    )


def test_provider_request_accounting_summarises_turn_session_and_provider(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    turn = _accept(coordinator, "turn-1", "Meter this")
    line_items = [
        {
            "provider_request_id": "provider-call-1",
            "parent_request_id": "turn-1",
            "phase": "planning",
            "engine": "deepseek-api",
            "model": "deepseek-v4-pro",
            "input": 100,
            "output": 20,
            "thinking": 5,
            "prompt_cache_hit_tokens": 40,
            "cost_usd": 0.01,
            "cost_source": "provider",
            "pricing_revision": "prices-v1",
        },
        {
            "provider_request_id": "provider-call-2",
            "parent_request_id": "turn-1",
            "phase": "compact",
            "engine": "hashi-api",
            "model": "gpt-5.6-luna",
            "input": 50,
            "output": 10,
            "cost_usd": 0.02,
            "cost_source": "pricing_table",
            "retry_count": 1,
            "compact": True,
            "pricing_revision": "prices-v1",
        },
    ]
    inserted = coordinator.store.record_provider_requests(
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        line_items=line_items,
    )
    assert inserted == 2
    assert coordinator.store.record_provider_requests(
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        line_items=[line_items[0]],
    ) == 0

    with pytest.raises(HerSessionStoreError) as exc_info:
        coordinator.store.record_provider_requests(
            session_id=turn.session_id,
            turn_id=turn.turn_id,
            line_items=[{**line_items[0], "input": 101}],
        )
    assert exc_info.value.code == "provider_request_conflict"

    summary = coordinator.store.usage_summary(turn.session_id)
    assert summary["total"] == {
        "provider_requests": 2,
        "input_tokens": 150,
        "output_tokens": 30,
        "thinking_tokens": 5,
        "prompt_cache_hit_tokens": 40,
        "prompt_cache_miss_tokens": 0,
        "cost_usd": 0.03,
        "retry_count": 1,
        "compact_requests": 1,
        "pricing_revisions": ["prices-v1"],
    }
    assert {(row["provider"], row["model"]) for row in summary["providers"]} == {
        ("deepseek-api", "deepseek-v4-pro"),
        ("hashi-api", "gpt-5.6-luna"),
    }

    # Immutable request facts and versioned monetary valuation have separate
    # authorities even though legacy request columns remain migration-safe.
    with sqlite3.connect(coordinator.store.path) as connection:
        request_row = connection.execute(
            """
            SELECT cost_usd, cost_source FROM her_provider_requests
            WHERE provider_request_id = 'provider-call-1'
            """
        ).fetchone()
        valuation_row = connection.execute(
            """
            SELECT pricing_revision, cost_usd, cost_source
            FROM her_provider_request_valuations
            WHERE provider_request_id = 'provider-call-1'
            """
        ).fetchone()
    assert request_row == (None, "separate_valuation")
    assert valuation_row == ("prices-v1", 0.01, "provider")


def test_pre_turn_accounting_session_promotes_without_losing_usage(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    binding = {
        "instance_id": "HASHI3",
        "agent_id": "agent1",
        "owner_id": "owner",
        "hashi_conversation_id": "conversation",
        "context_generation": 1,
        "workzone_identity": "workzone",
    }
    shell = coordinator.store.ensure_accounting_session(
        session_id="session-1",
        **binding,
    )
    assert shell["status"] == "accounting"
    assert shell["state_version"] == 0
    assert shell["last_turn_id"] is None

    compact_usage = {
        "provider_request_id": "compact-before-turn-1",
        "parent_request_id": "compact:1",
        "phase": "compact",
        "engine": "deepseek-api",
        "model": "deepseek-v4-flash",
        "input": 120,
        "output": 30,
        "compact": True,
        "pricing_revision": "prices-v1",
    }
    assert coordinator.store.record_provider_requests(
        session_id="session-1",
        turn_id="",
        line_items=[compact_usage],
    ) == 1

    accepted = _accept(coordinator, "turn-1", "Open after Compact")
    promoted = coordinator.store.session("session-1")

    assert accepted.session_id == "session-1"
    assert promoted["status"] == "open"
    assert promoted["state_version"] == 1
    assert promoted["last_turn_id"] == "turn-1"
    assert coordinator.store.usage_summary("session-1")["total"] == {
        "provider_requests": 1,
        "input_tokens": 120,
        "output_tokens": 30,
        "thinking_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
        "cost_usd": None,
        "retry_count": 0,
        "compact_requests": 1,
        "pricing_revisions": ["prices-v1"],
    }

    # Repeated preflight is idempotent and cannot duplicate immutable usage.
    assert coordinator.store.ensure_accounting_session(
        session_id="session-1",
        **binding,
    )["status"] == "open"
    assert coordinator.store.record_provider_requests(
        session_id="session-1",
        turn_id="",
        line_items=[compact_usage],
    ) == 0


def test_provider_request_is_durable_before_active_turn_completes(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    turn = _accept(coordinator, "turn-1", "Meter immediately")
    adapter_surface = SimpleNamespace(_session_coordinator=coordinator)
    observer = HERv2Adapter._fixed_provider_usage_observer(
        adapter_surface,
        accepted=turn,
        turn_config=SimpleNamespace(
            routing_revision=7,
            capability_revision=3,
            pricing_revision="prices-v7",
        ),
    )
    provider = HashiStageProvider(
        backend_manager=object(),
        usage_observer=observer,
        default_recovery_kind="active_turn_recovery:unknown_side_effect",
    )
    provider._record_usage_line_item(
        request_id="hashi-request:turn-1",
        phase="strategy",
        engine="deepseek-api",
        model="deepseek-v4-pro",
        response=SimpleNamespace(
            stream_metadata={},
            usage=SimpleNamespace(
                input_tokens=80,
                output_tokens=12,
                thinking_tokens=4,
            ),
            cost_usd=0.004,
        ),
        invocation_id="turn-1:strategy:invocation:1",
        attempt=1,
    )

    assert coordinator.store.active_turn_recovery(
        turn.session_id, turn.turn_id
    )["status"] == "active"
    summary = coordinator.store.usage_summary(turn.session_id)
    assert summary["total"]["provider_requests"] == 1
    assert summary["total"]["pricing_revisions"] == ["prices-v7"]
    with sqlite3.connect(coordinator.store.path) as connection:
        recovery_kind = connection.execute(
            """
            SELECT recovery_kind FROM her_provider_requests
            WHERE session_id = ?
            """,
            (turn.session_id,),
        ).fetchone()[0]
    assert recovery_kind == "active_turn_recovery:unknown_side_effect"


def test_physical_call_observer_writes_immediately_and_final_response_dedupes(
    tmp_path,
):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    turn = _accept(coordinator, "turn-physical", "Meter the wire request")
    adapter_surface = SimpleNamespace(_session_coordinator=coordinator)
    audit_path = tmp_path / "logs" / "her_v2_audit.jsonl"
    provider = HashiStageProvider(
        backend_manager=object(),
        audit_log=DurableAuditLog(
            primary_path=audit_path,
            fallback_path=tmp_path / "logs" / "her_v2_audit.fallback.jsonl",
        ),
        usage_observer=HERv2Adapter._fixed_provider_usage_observer(
            adapter_surface,
            accepted=turn,
            turn_config=SimpleNamespace(
                routing_revision=9,
                capability_revision=4,
                pricing_revision="prices-v9",
            ),
        ),
    )
    backend = SimpleNamespace()

    def set_observer(callback):
        backend.provider_call_observer = callback

    backend.set_provider_call_observer = set_observer
    provider._bind_provider_call_observer(
        backend,
        request_id="hashi-request:turn-physical",
        phase="execution",
        engine="hashi-api",
        model="gpt-5.6-luna",
        invocation_id="turn-physical:execution:1",
    )
    physical_call = {
        "provider_request_id": "physical-call-1",
        "input": 100,
        "output": 20,
        "thinking": 5,
        "token_source": "provider",
        "thinking_in_output": True,
        "cost_usd": 0.01,
        "prompt_cache_hit_tokens": 70,
        "prompt_cache_miss_tokens": 30,
        "provider_call_latency_ms": 12.5,
        "attempt": 1,
        "retry_count": 0,
        "recovery_kind": "none",
        "status": "completed",
        "call_serial": 1,
        "raw_finish_reason_present": True,
        "raw_finish_reason": "tool_calls",
        "normalized_finish_reason": "tool_calls",
        "finish_reason_source": "provider",
        "transport_complete": True,
        "transport_state": "done_marker",
        "stream_done": True,
        "stream_eof": False,
        "tool_calls": [
            {
                "id": "call-1",
                "name": "file_read",
                "complete": True,
                "arguments_state": "valid_object",
            }
        ],
        "reasoning_availability": "unavailable",
        "decision": "execute_tools",
        "decision_reason": "complete_structured_tool_calls",
        "decision_success": False,
    }

    backend.provider_call_observer(physical_call)

    # The Turn is still active, yet the physical request is already durable.
    assert coordinator.store.usage_summary(turn.session_id)["total"][
        "provider_requests"
    ] == 1
    audit_records = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(audit_records) == 1
    assert audit_records[0]["turn_id"] == "turn-physical"
    assert audit_records[0]["request_ref"] == "hashi-request:turn-physical"
    assert audit_records[0]["event"] == "provider_physical_response_decision"
    assert audit_records[0]["payload"]["raw_finish_reason"] == "tool_calls"
    assert audit_records[0]["payload"]["decision"] == "execute_tools"
    assert audit_records[0]["payload"]["tool_calls"][0]["id"] == "call-1"
    provider._record_usage_line_item(
        request_id="hashi-request:turn-physical",
        phase="execution",
        engine="hashi-api",
        model="gpt-5.6-luna",
        response=SimpleNamespace(
            stream_metadata={"meter": {"provider_calls": [physical_call]}},
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=20,
                thinking_tokens=5,
            ),
            cost_usd=0.01,
            is_success=True,
        ),
        invocation_id="turn-physical:execution:1",
    )
    assert len(provider.usage_line_items) == 1
    assert coordinator.store.usage_summary(turn.session_id)["total"][
        "provider_requests"
    ] == 1


def test_legacy_inline_costs_migrate_to_versioned_valuations(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    turn = _accept(coordinator, "turn-1", "Migrate meter data")
    coordinator.store.record_provider_requests(
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        line_items=[
            {
                "provider_request_id": "legacy-call",
                "engine": "deepseek-api",
                "model": "deepseek-v4-pro",
                "input": 10,
                "output": 2,
                "cost_usd": 0.123,
                "cost_source": "provider",
                "pricing_revision": "legacy-v1",
            }
        ],
    )
    with sqlite3.connect(coordinator.store.path) as connection:
        connection.execute("DROP TABLE her_provider_request_valuations")
        connection.execute(
            """
            UPDATE her_provider_requests
            SET cost_usd = 0.123, cost_source = 'provider'
            WHERE provider_request_id = 'legacy-call'
            """
        )

    migrated = HerSessionStore(coordinator.store.path)
    summary = migrated.usage_summary(turn.session_id)

    assert summary["total"]["cost_usd"] == 0.123
    assert summary["total"]["pricing_revisions"] == ["legacy-v1"]
