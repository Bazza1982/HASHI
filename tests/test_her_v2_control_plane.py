from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from adapters.her_v2 import HERv2Adapter
from adapters.her_v2_provider import HashiStageProvider
from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator
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

    bounded = HerSessionStore._bounded_side_effects(effects, limit=32)

    assert len(bounded) == 32
    assert {row["tool_call_id"] for row in bounded} >= {
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

    bounded = HerSessionStore._bounded_tool_receipts(receipts, effects, limit=32)

    assert len(bounded) == 32
    assert bounded[0]["operation_id"] == "oldest-unknown-operation"


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
    provider = HashiStageProvider(
        backend_manager=object(),
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
    }

    backend.provider_call_observer(physical_call)

    # The Turn is still active, yet the physical request is already durable.
    assert coordinator.store.usage_summary(turn.session_id)["total"][
        "provider_requests"
    ] == 1
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
