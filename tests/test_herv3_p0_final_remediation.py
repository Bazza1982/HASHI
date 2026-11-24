"""Recovery projection and preflight regressions for PR #203 final review."""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from adapters.openrouter_api import OpenRouterAdapter
from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator
from orchestrator.her_v2.recovery_projection import (
    FIELD_BUDGET_CHARS,
    TOTAL_BUDGET_CHARS,
    bounded_prompt_projection,
)
from orchestrator.her_v2.wip_journal import WIPJournal

from tests.test_herv3_p0_checkpoint_recovery_bounds import (
    _accept, _route_snapshot, _runtime_event, _settle,
    MAIN, ALT,
)


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def test_unicode_projection_preserves_critical_state_at_whole_budget():
    context = {
        "session_id": "session-unicode",
        "interrupted_turn_id": "turn-unicode",
        "safe_to_resume": False,
        "recovery_disposition": "UNKNOWN_SIDE_EFFECT",
        "tool_receipts": [{"operation_id": f"read-{n}", "status": "completed"} for n in range(20)],
        "side_effects": [{"operation_id": f"write-{n}", "state": "unknown"} for n in range(23)],
        "remaining_work": {"goal": "目标🌙" * 30000, "status": "in_progress"},
        **{f"history_{n}": "历史🌙" * 20000 for n in range(8)},
    }
    projection = bounded_prompt_projection(context)
    assert projection["safe_to_resume"] is False
    assert projection["recovery_disposition"] == "UNKNOWN_SIDE_EFFECT"
    assert projection["interrupted_turn_id"] == "turn-unicode"
    assert projection["side_effects"]["unresolved_total"] == 23
    assert projection["side_effects"]["evidence_ref"]
    assert _bytes(projection) <= TOTAL_BUDGET_CHARS
    assert all(_bytes({key: value}) <= FIELD_BUDGET_CHARS for key, value in projection.items())


@pytest.mark.parametrize("value", ["历史" * 5000, "🌙" * 8000, '\\n"\\\\' * 6000])
def test_every_utf8_field_includes_its_json_wrapper_in_budget(value):
    context = {
        "session_id": "s", "interrupted_turn_id": "t",
        "safe_to_resume": False, "recovery_disposition": "UNKNOWN_SIDE_EFFECT",
        "provider_context_checkpoint": {"digest": value},
        "strategy": value, "side_effects": [{"state": "unknown"}],
    }
    projection = bounded_prompt_projection(context)
    assert "provider_context_checkpoint" in projection
    assert all(_bytes({key: field}) <= FIELD_BUDGET_CHARS for key, field in projection.items())
    assert projection["side_effects"]["unresolved_total"] == 1


@pytest.mark.parametrize("count", [0, 1, 20, 1000])
def test_projection_preserves_receipts_and_unknown_effects_when_projected_again(count):
    context = {
        "session_id": "session-1",
        "interrupted_turn_id": "turn-1",
        "safe_to_resume": False,
        "recovery_disposition": "UNKNOWN_SIDE_EFFECT",
        "tool_receipts": [{"operation_id": f"r-{n}", "status": "completed"} for n in range(count)],
        "side_effects": [{"operation_id": f"s-{n}", "state": "unknown"} for n in range(25)],
    }
    projected = bounded_prompt_projection(context)
    repeated = bounded_prompt_projection(projected)
    assert repeated["tool_receipts"]["total"] == count
    assert repeated["side_effects"]["unresolved_total"] == 25
    assert repeated["safe_to_resume"] is False


def test_request_received_uses_authoritative_user_goal_and_retains_audit(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    turn = _accept(coordinator, "goal-1", "真实用户目标")
    provider_prompt = "SYSTEM: assembled context\n" + "previous recovery " * 2000
    coordinator.store.record_runtime_event(
        session_id=turn.session_id, turn_id=turn.turn_id,
        record=_runtime_event(turn.turn_id, "goal-1:request", "request_received", request=provider_prompt),
    )
    state = coordinator.store.active_turn_recovery(turn.session_id, turn.turn_id)
    assert state["remaining_work"]["goal"] == "真实用户目标"
    with sqlite3.connect(coordinator.store.path) as conn:
        row = conn.execute("SELECT payload_json FROM her_session_events WHERE event_id=?", ("runtime:goal-1:request",)).fetchone()
    assert json.loads(row[0])["facts"]["request"] == provider_prompt


def test_active_recovery_reference_resolves_actual_sqlite_row(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    turn = _accept(coordinator, "source", "执行")
    coordinator.store.record_runtime_event(
        session_id=turn.session_id, turn_id=turn.turn_id,
        record=_runtime_event(turn.turn_id, "source:intent", "tool_intent", operation_id="write-1",
                              tool_call_id="write-1", tool_name="write", read_only=False),
    )
    assert coordinator.store.reconcile_interrupted() == 1
    successor = _accept(coordinator, "successor", "调查")
    context = coordinator.store.consume_recovery_context(
        session_id=successor.session_id, current_turn_id=successor.turn_id)
    projected = bounded_prompt_projection(context)
    kind, field, session, source_turn = projected["side_effects"]["evidence_ref"].split(":")
    assert (kind, field, session, source_turn) == (
        "her_active_turn_recovery", "side_effects", turn.session_id, turn.turn_id)
    with sqlite3.connect(coordinator.store.path) as conn:
        row = conn.execute(
            "SELECT side_effects_json FROM her_active_turn_recovery WHERE session_id=? AND turn_id=?",
            (session, source_turn),
        ).fetchone()
    assert row and json.loads(row[0])[0]["operation_id"] == "write-1"


def test_provider_checkpoint_reference_queries_segmented_messages(tmp_path):
    from tests.test_her_v2_control_plane import _run_claim

    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    turn = _accept(coordinator, "provider-source", "Inspect")
    coordinator.record_provider_context_checkpoint(turn, {
        "provider": "openrouter-api", "model": "test-model", "stage": "direct",
        "boundary": "completed_provider_response",
        "messages": [{"role": "user", "content": "ORIGINAL PROVIDER MESSAGE"}],
        "producer_run_claim": _run_claim("run-source", "provider-source"),
    })
    assert coordinator.store.reconcile_interrupted() == 1
    successor = _accept(coordinator, "provider-successor", "Recover")
    recovery = coordinator.store.consume_recovery_context(
        session_id=successor.session_id, current_turn_id=successor.turn_id)
    projection = bounded_prompt_projection(recovery)
    view = projection["provider_context_checkpoint"]
    assert view["present"] is True
    assert bounded_prompt_projection(projection)["provider_context_checkpoint"] == view
    kind, session, source_turn = view["items_evidence_ref"].split(":")
    assert (kind, session, source_turn) == ("her_provider_context_items", turn.session_id, turn.turn_id)
    with sqlite3.connect(coordinator.store.path) as conn:
        rows = conn.execute(
            "SELECT payload_json FROM her_provider_context_items WHERE session_id=? AND turn_id=?",
            (session, source_turn),
        ).fetchall()
    assert any(json.loads(row[0]).get("content") == "ORIGINAL PROVIDER MESSAGE" for row in rows)


def test_unavailable_provider_checkpoint_is_not_claimed_as_present():
    view = bounded_prompt_projection({
        "session_id": "s", "interrupted_turn_id": "t",
        "provider_context_checkpoint": {"available": False, "message_count": 0, "reasoning_count": 0},
    })["provider_context_checkpoint"]
    assert view["present"] is False
    assert "items_evidence_ref" not in view


def test_unavailable_provider_checkpoint_stays_unavailable_after_reprojection(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    source = _accept(coordinator, "missing-provider-source", "Investigate")
    coordinator.store.record_runtime_event(
        session_id=source.session_id, turn_id=source.turn_id,
        record=_runtime_event(
            source.turn_id, "missing-provider-source:intent", "tool_intent",
            operation_id="write-unknown", tool_call_id="write-unknown",
            tool_name="write", read_only=False,
        ),
    )
    assert coordinator.store.reconcile_interrupted() == 1
    successor = _accept(coordinator, "missing-provider-successor", "Recover")
    context = coordinator.store.consume_recovery_context(
        session_id=successor.session_id, current_turn_id=successor.turn_id,
    )
    assert context["provider_context_checkpoint"]["available"] is False
    for _ in range(3):
        context = bounded_prompt_projection(context)
        checkpoint = context["provider_context_checkpoint"]
        assert checkpoint["present"] is False
        assert "items_evidence_ref" not in checkpoint
        assert context["safe_to_resume"] is False
        assert context["side_effects"]["unresolved_total"] == 1


@pytest.mark.parametrize("present", [None, "yes", 1])
def test_malformed_projected_checkpoint_does_not_claim_presence(present):
    projection = bounded_prompt_projection({
        "session_id": "s", "interrupted_turn_id": "t",
        "provider_context_checkpoint": {
            "present": present,
            "items_evidence_ref": "her_provider_context_items:s:t",
        },
    })
    checkpoint = projection["provider_context_checkpoint"]
    assert checkpoint["present"] is False
    assert "items_evidence_ref" not in checkpoint


def test_truncated_projected_checkpoint_never_becomes_present_again():
    context = {
        "session_id": "s", "interrupted_turn_id": "t", "safe_to_resume": False,
        "provider_context_checkpoint": {
            "present": False, "items_evidence_ref": "her_provider_context_items:s:t",
            "unexpected_history": "oversized" * 10000,
        },
    }
    for _ in range(3):
        context = bounded_prompt_projection(context)
        checkpoint = context["provider_context_checkpoint"]
        assert checkpoint.get("present") is not True
        assert "items_evidence_ref" not in checkpoint
        assert context["safe_to_resume"] is False


def test_settled_checkpoint_receipt_reference_resolves_actual_record(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    turn = _accept(coordinator, "settled", "read")
    coordinator.store.record_runtime_event(
        session_id=turn.session_id, turn_id=turn.turn_id,
        record=_runtime_event(turn.turn_id, "settled:receipt", "tool_receipt",
                              receipt={"operation_id": "read-1", "status": "completed", "completed": True}),
    )
    _settle(coordinator, turn, route_snapshot=_route_snapshot(*MAIN))
    checkpoint = coordinator.store.settled_checkpoint(turn.session_id)["payload"]
    projection = bounded_prompt_projection(checkpoint)
    ref = projection["tool_receipts"]["evidence_ref"]
    assert ref.startswith("her_settled_checkpoints:")
    with sqlite3.connect(coordinator.store.path) as conn:
        row = conn.execute("SELECT payload_json FROM her_settled_checkpoints WHERE session_id=?",
                           (checkpoint["session_id"],)).fetchone()
    assert row and json.loads(row[0])["tool_receipts"][0]["operation_id"] == "read-1"

    # Once the latest checkpoint advances, the per-Turn evidence still resolves.
    second = _accept(coordinator, "settled-next", "continue")
    _settle(coordinator, second, route_snapshot=_route_snapshot(*MAIN))
    kind, field, session, source_turn = projection["tool_receipts"]["canonical_evidence_ref"].split(":")
    assert (kind, field, session, source_turn) == (
        "her_active_turn_recovery", "tool_receipts", turn.session_id, turn.turn_id)
    with sqlite3.connect(coordinator.store.path) as conn:
        row = conn.execute(
            "SELECT tool_receipts_json FROM her_active_turn_recovery WHERE session_id=? AND turn_id=?",
            (session, source_turn),
        ).fetchone()
    assert row and json.loads(row[0])[0]["operation_id"] == "read-1"


def test_profile_reordering_does_not_rebuild_but_main_route_change_does(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    first = _accept(coordinator, "route-1", "Start")
    snapshot = _route_snapshot(*MAIN, auxiliary=("aux", "secondary"))
    _settle(coordinator, first, route_snapshot=snapshot)
    second = _accept(coordinator, "route-2", "Continue")
    reordered = _route_snapshot(*MAIN, auxiliary=("aux", "changed"))
    reordered["profiles"].reverse()
    frozen = coordinator.store.freeze_turn_routing(
        session_id=second.session_id, turn_id=second.turn_id, routing_revision=2,
        capability_revision=1, pricing_revision="prices-v1", route_snapshot=reordered)
    assert frozen["rebuild_from_checkpoint"] is False
    coordinator.complete(second, assistant_text="Done")
    third = _accept(coordinator, "route-3", "Switch")
    switched = _route_snapshot(*ALT, auxiliary=("aux", "changed"))
    switched["profiles"].reverse()
    frozen = coordinator.store.freeze_turn_routing(
        session_id=third.session_id, turn_id=third.turn_id, routing_revision=3,
        capability_revision=1, pricing_revision="prices-v1", route_snapshot=switched)
    assert frozen["rebuild_from_checkpoint"] is True


@pytest.mark.asyncio
async def test_real_configuration_model_switch_rebuilds_but_reordering_and_effort_do_not(tmp_path):
    from dataclasses import replace
    from adapters.her_v2 import HERv2Adapter
    from orchestrator.her_v2.models import Route, Stage
    from tests.test_her_v2_adapter import _agent_config, _global_config, _DirectProvider
    from tests.test_her_v2_runtime_configuration import _manager

    config = _agent_config(tmp_path / "adapter")
    provider = _DirectProvider()
    config._her_v2_stage_provider = provider
    runtime = SimpleNamespace(backend_manager=SimpleNamespace(agent_mode="fixed"),
                              _request_meta_by_id={}, current_request_meta={})
    config._hashi_runtime = runtime
    adapter = HERv2Adapter(config, _global_config(tmp_path))
    manager = _manager(tmp_path / "manager")
    manager.current_backend = adapter
    assert await adapter.initialize()
    manager.apply_her_v3_target(manager.get_her_v3_target())
    sections = [{"key": "permanent_system", "title": "SYSTEM", "text": "Stable",
                 "authority": "permanent_system", "order": 0}]
    payload = {"transport_snapshot": {"version": 1, "sections": sections}}

    async def run_turn(index):
        request_id = f"route-real-{index}"
        meta = {"request_id": request_id, "hashi_session_id": "routing-session",
                "hashi_message_id": f"routing-message-{index}", "context_generation": 1,
                "session_workspace": str(tmp_path / "routing-session")}
        runtime._request_meta_by_id[request_id] = meta
        runtime.current_request_meta = meta
        transport, _ = adapter.prepare_fixed_turn_input(
            prompt_payload=payload, user_message=f"Task {index}",
            request_id=request_id, request_meta=meta)
        response = await adapter.generate_response(transport, request_id)
        assert response.is_success
        return adapter._session_coordinator.store.session(adapter._session_id)

    try:
        first = await run_turn(1)
        original = adapter._v2_config.profile_for_route(Route.DIRECT)
        adapter._v2_config = replace(adapter._v2_config, profiles=dict(reversed(list(adapter._v2_config.profiles.items()))))
        adapter.effort = "low"
        second = await run_turn(2)
        assert second["provider_context_generation"] == first["provider_context_generation"]

        manager.apply_her_v3_target(manager.prepare_her_v3_provider("openrouter-api"))
        changed = adapter._v2_config.profile_for_route(Route.DIRECT)
        assert (changed.engine, changed.model) != (original.engine, original.model)
        third = await run_turn(3)
        assert third["provider_context_generation"] == second["provider_context_generation"] + 1
        direct_profiles = [profile for profile, request in provider.requests if request.stage is Stage.DIRECT]
        assert (direct_profiles[-1].engine, direct_profiles[-1].model) == (changed.engine, changed.model)
    finally:
        await adapter.shutdown()


def test_wip_summary_and_many_records_have_bounded_model_context(tmp_path):
    journal = WIPJournal(tmp_path / "wip.jsonl")
    journal.begin_turn(request_id="r0", prompt="prompt", request_summary="中文🌙" * 30000)
    record = journal.records()[0]
    assert len(record["request_summary"].encode("utf-8")) <= 2048
    assert record["request_summary_full"] == "中文🌙" * 30000
    capsule = journal.recovery_capsule(journal.records())
    assert capsule["unfinished_requests"][0]["request_summary_full"] == "中文🌙" * 30000
    for n in range(100):
        journal.append_audit({"event": "tool_completed", "event_id": f"e{n}",
                              "payload": {"text": "证据🌙" * 500}})
    rendered = journal.render_context(journal.records())
    assert len(rendered.encode("utf-8")) <= TOTAL_BUDGET_CHARS
    projection = json.JSONDecoder().raw_decode(rendered.split("\n\n", 1)[1])[0]
    source = projection["evidence_ref"].removeprefix("wip_journal:")
    from pathlib import Path
    original = [json.loads(line) for line in Path(source).read_text(encoding="utf-8").splitlines()]
    assert len(original) == 101
    assert original[0]["request_summary_full"] == "中文🌙" * 30000
    assert original[-1]["event_id"] == "e99"


def test_uncertain_conservative_estimate_does_not_reject_plausibly_fitting_text(monkeypatch):
    adapter = OpenRouterAdapter.__new__(OpenRouterAdapter)
    adapter.config = SimpleNamespace(engine="openrouter-api", model="test", _hashi_runtime=object())
    monkeypatch.setattr("orchestrator.context_compaction.resolve_capacity_profile",
        lambda *a, **k: SimpleNamespace(provider="openrouter-api", model="test", usable_input_tokens=3000))
    adapter._preflight_payload_capacity({"model": "test", "messages": [{"role": "user", "content": "hello " * 1400}]})


def test_five_consecutive_real_recovery_handoffs_do_not_nest_or_replay(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    effect_file = tmp_path / "effects.txt"
    first = _accept(coordinator, "chain-0", "原始用户目标")
    effect_file.write_text("done-once\nunknown-once\n", encoding="utf-8")
    for event_id, event, payload in [
        ("done:intent", "tool_intent", {"operation_id": "done", "tool_call_id": "done", "tool_name": "write", "read_only": False}),
        ("done:receipt", "tool_receipt", {"operation_id": "done", "receipt": {"operation_id": "done", "status": "completed", "completed": True}}),
        ("unknown:intent", "tool_intent", {"operation_id": "unknown", "tool_call_id": "unknown", "tool_name": "write", "read_only": False}),
    ]:
        coordinator.store.record_runtime_event(
            session_id=first.session_id, turn_id=first.turn_id,
            record=_runtime_event(first.turn_id, event_id, event, **payload))
    previous = first
    sizes = []
    for n in range(1, 6):
        assert coordinator.store.reconcile_interrupted() == 1
        successor = _accept(coordinator, f"chain-{n}", f"继续调查 {n}")
        recovery = coordinator.store.consume_recovery_context(session_id=successor.session_id, current_turn_id=successor.turn_id)
        projected = bounded_prompt_projection(recovery)
        assert projected["safe_to_resume"] is False
        assert projected["side_effects"]["unresolved_total"] == 1
        assert projected["tool_receipts"]["total"] == 1
        assert projected["remaining_work"]["goal"] == "原始用户目标"
        prompt = successor.materialized_prompt + json.dumps(projected, ensure_ascii=False)
        sizes.append(len(prompt.encode("utf-8")))
        coordinator.store.record_runtime_event(
            session_id=successor.session_id, turn_id=successor.turn_id,
            record=_runtime_event(successor.turn_id, f"chain-{n}:request", "request_received", request=prompt))
        remaining = coordinator.store.active_turn_recovery(successor.session_id, successor.turn_id)["remaining_work"]
        assert remaining["goal"] == f"继续调查 {n}"
        assert json.dumps(remaining, ensure_ascii=False).count("inherited_recovery") <= 1
        previous = successor
    assert max(sizes) < 100000
    assert max(sizes) - min(sizes) < 4096
    assert effect_file.read_text(encoding="utf-8").splitlines() == ["done-once", "unknown-once"]
    with sqlite3.connect(coordinator.store.path) as conn:
        rows = conn.execute("SELECT event_id FROM her_session_events WHERE session_id=? AND event_id LIKE 'runtime:%'", (first.session_id,)).fetchall()
    assert len(rows) >= 8


@pytest.mark.asyncio
async def test_adapter_runs_five_failed_recoveries_with_bounded_typed_context(tmp_path):
    from adapters.her_v2 import HERv2Adapter
    from orchestrator.her_v2.v3_prompt import compile_main_prompt
    from tests.test_her_v2_adapter import _agent_config, _global_config

    config = _agent_config(tmp_path)
    runtime = SimpleNamespace(backend_manager=SimpleNamespace(agent_mode="fixed"),
                              _request_meta_by_id={}, current_request_meta={})
    config._hashi_runtime = runtime
    effect_file = tmp_path / "effect-log.txt"
    observed = []

    class FailingProvider:
        async def invoke(self, profile, request):
            accepted = adapter._canonical_active_turns[request.request_ref]
            if not observed:
                for event_id, event, payload in [
                    ("adapter-done:intent", "tool_intent", {"operation_id": "done", "tool_call_id": "done", "tool_name": "write", "read_only": False}),
                    ("adapter-done:receipt", "tool_receipt", {"operation_id": "done", "receipt": {"operation_id": "done", "status": "completed", "completed": True}}),
                    ("adapter-unknown:intent", "tool_intent", {"operation_id": "unknown", "tool_call_id": "unknown", "tool_name": "write", "read_only": False}),
                ]:
                    adapter._session_coordinator.record_runtime_event(
                        accepted, _runtime_event(accepted.turn_id, event_id, event, **payload))
                effect_file.write_text("done-once\nunknown-once\n", encoding="utf-8")
            system, user = compile_main_prompt(
                pcm_input=request.context["pcm_input"],
                fallback_request=request.goal,
                context=request.context,
            )
            observed.append((len((system + user).encode("utf-8")), request.context["pcm_input"], request.request_ref))
            raise RuntimeError("simulated interrupted provider")

    config._her_v2_stage_provider = FailingProvider()
    adapter = HERv2Adapter(config, _global_config(tmp_path))
    assert await adapter.initialize()
    sections = [{"key": "permanent_system", "title": "SYSTEM", "text": "Stable",
                 "authority": "permanent_system", "order": 0}]
    prompt_payload = {"transport_snapshot": {"version": 1, "sections": sections}}
    try:
        for index in range(6):
            request_id = f"adapter-chain-{index}"
            meta = {"request_id": request_id, "hashi_session_id": "adapter-session",
                    "hashi_message_id": f"message-{index}", "context_generation": 1,
                    "session_workspace": str(tmp_path / "adapter-session")}
            runtime._request_meta_by_id[request_id] = meta
            runtime.current_request_meta = meta
            transport, _ = adapter.prepare_fixed_turn_input(
                prompt_payload=prompt_payload, user_message=f"调查 {index}",
                request_id=request_id, request_meta=meta)
            response = await adapter.generate_response(transport, request_id)
            assert not response.is_success
        assert {ref for _, _, ref in observed} == {f"hashi-request:adapter-chain-{n}" for n in range(6)}
        for size, pcm, ref in observed:
            if ref == "hashi-request:adapter-chain-0":
                continue
            recovery = pcm["runtime_context"]["active_turn_recovery"]
            assert recovery["safe_to_resume"] is False
            assert recovery["side_effects"]["unresolved_total"] == 1
            assert recovery["tool_receipts"]["total"] == 1
            assert recovery["remaining_work"]["goal"] == "调查 0"
            assert size < 100000
        assert max(size for size, _, _ in observed) - min(size for size, _, _ in observed) < 10000
        assert effect_file.read_text(encoding="utf-8").splitlines() == ["done-once", "unknown-once"]
    finally:
        await adapter.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "hello " * 1400,
    "中文记录🌙" * 3000,
    json.dumps({"items": [{"name": "alpha", "value": n} for n in range(800)]}),
    "def compute(value):\n    return value + 1\n" * 800,
    "long ordinary request with data: and base64, text " * 1500,
])
async def test_provider_pipeline_allows_uncertain_text_and_calls_capacity_preparation(tmp_path, monkeypatch, text):
    from unittest.mock import AsyncMock
    from adapters.openrouter_api import _APIResult
    from tests.test_openrouter_multimodal import _adapter

    adapter = _adapter(tmp_path)
    adapter.config._hashi_runtime = object()
    monkeypatch.setattr("orchestrator.context_compaction.resolve_capacity_profile",
        lambda *a, **k: SimpleNamespace(provider="openrouter-api", model=adapter.config.model, usable_input_tokens=3000))
    adapter._prepare_payload_capacity = AsyncMock(return_value=None)
    adapter._call_api_once = AsyncMock(return_value=_APIResult("accepted", None, "stop", 10, 2))
    try:
        result = await adapter.generate_response(text, "uncertain-capacity")
        assert result.is_success
        assert adapter._prepare_payload_capacity.await_count == 1
        sent = adapter._call_api_once.call_args.args[0]
        assert any(text in str(message.get("content") or "") for message in sent["messages"])
    finally:
        await adapter.shutdown()


@pytest.mark.asyncio
async def test_structured_image_and_audio_reach_provider_without_byte_guessing(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from adapters.openrouter_api import _APIResult
    from tests.test_openrouter_multimodal import _adapter

    adapter = _adapter(tmp_path)
    adapter.config._hashi_runtime = object()
    monkeypatch.setattr("orchestrator.context_compaction.resolve_capacity_profile",
        lambda *a, **k: SimpleNamespace(provider="openrouter-api", model=adapter.config.model, usable_input_tokens=1000))
    payload = {
        "model": adapter.config.model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "描述 data: in ordinary text"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 100000}},
            {"type": "input_audio", "input_audio": {"data": "B" * 100000, "format": "wav"}},
        ]}],
    }
    adapter._build_payload = lambda *args, **kwargs: payload
    adapter._call_api_once = AsyncMock(return_value=_APIResult("accepted", None, "stop", 10, 2))
    try:
        result = await adapter.generate_response("media", "media-capacity")
        assert result.is_success
        sent = adapter._call_api_once.call_args.args[0]
        assert sent["messages"][0]["content"] == payload["messages"][0]["content"]
    finally:
        await adapter.shutdown()


@pytest.mark.asyncio
async def test_capacity_preparation_runs_before_provider_specific_final_check(tmp_path):
    from unittest.mock import AsyncMock
    from adapters.openrouter_api import _APIResult, ProviderContextCapacityPreflightError
    from tests.test_openrouter_multimodal import _adapter

    adapter = _adapter(tmp_path)
    calls = []

    async def prepare(payload, **kwargs):
        calls.append("prepare")
        changed = dict(payload)
        changed["messages"] = [{"role": "user", "content": "compacted"}]
        return changed

    def exact_check(payload):
        calls.append("check")
        if "original oversized input" in json.dumps(payload):
            raise ProviderContextCapacityPreflightError("needs preparation")

    adapter._prepare_payload_capacity = prepare
    adapter._preflight_payload_capacity = exact_check
    adapter._call_api_once = AsyncMock(return_value=_APIResult("accepted", None, "stop", 10, 2))
    try:
        result = await adapter.generate_response("original oversized input", "capacity-order")
        assert result.is_success
        assert calls == ["prepare", "check"]
        assert adapter._call_api_once.call_args.args[0]["messages"][0]["content"] == "compacted"
    finally:
        await adapter.shutdown()


def test_old_polluted_recovery_is_projected_without_modifying_original_row(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    source = _accept(coordinator, "old-source", "原始任务")
    assert coordinator.store.reconcile_interrupted() == 1
    polluted = {"goal": "旧的展开提示" * 20000, "inherited_recovery": {"goal": "nested"}}
    with sqlite3.connect(coordinator.store.path) as conn:
        conn.execute(
            "UPDATE her_active_turn_recovery SET remaining_work_json=? WHERE session_id=? AND turn_id=?",
            (json.dumps(polluted, ensure_ascii=False), source.session_id, source.turn_id),
        )
    successor = _accept(coordinator, "old-successor", "恢复")
    context = coordinator.store.consume_recovery_context(
        session_id=successor.session_id, current_turn_id=successor.turn_id)
    view = bounded_prompt_projection(context)
    assert view["remaining_work"]["goal"] == "原始任务"
    assert "旧的展开提示" not in json.dumps(view, ensure_ascii=False)
    with sqlite3.connect(coordinator.store.path) as conn:
        original = json.loads(conn.execute(
            "SELECT remaining_work_json FROM her_active_turn_recovery WHERE session_id=? AND turn_id=?",
            (source.session_id, source.turn_id),
        ).fetchone()[0])
    assert original == polluted


def test_compacted_wip_prompt_reference_resolves_full_memory_record(tmp_path):
    from orchestrator.bridge_memory import BridgeMemoryStore
    from orchestrator.context_compaction import _render_timeline_entry

    memory = BridgeMemoryStore(tmp_path)
    capsule = {
        "format": "hashi-her-v2-wip-recovery-capsule-v1",
        "unfinished_requests": [{"request_id": str(i), "request_summary": "历史🌙" * 1000} for i in range(120)],
    }
    original = json.dumps(capsule, ensure_ascii=False)
    row_id = memory.record_recovery_capsule(original, origin_ref="sha256:test-large-wip")
    rendered = _render_timeline_entry(
        {"kind": "recovery_capsule", "turn_ids": [row_id], "content": original,
         "source": "wip-recovery:sha256:test-large-wip"},
        immediate=False, display_timezone="UTC", naive_timezone=None)
    assert len(rendered.encode("utf-8")) < TOTAL_BUDGET_CHARS
    assert f"bridge_memory:turns:{row_id}" in rendered
    with sqlite3.connect(memory.db_path) as conn:
        saved = conn.execute("SELECT text FROM turns WHERE id=?", (row_id,)).fetchone()
    assert saved and json.loads(saved[0]) == capsule


def test_many_compacted_recovery_records_have_one_bounded_history_view(tmp_path):
    import re
    from orchestrator.context_compaction import HistorySnapshot, render_history

    rows = tuple({
        "id": n, "role": "recovery", "source": f"wip-recovery:sha256:{n}",
        "text": json.dumps({"format": "hashi-her-v2-wip-recovery-capsule-v1",
                            "summary": f"record-{n}-" + "历史🌙" * 3000}, ensure_ascii=False),
        "ts": "2026-11-23T00:00:00Z",
    } for n in range(1, 31))
    snapshot = HistorySnapshot(
        generation=1, active_capsule_hash="", active_capsule_ref="", active_capsule=None,
        active_record=None, covered_through_turn_id=0, all_turns=rows,
        eligible_turns=(), recent_turns=rows, source_digest="", recent_exchanges=30,
    )
    rendered = render_history(snapshot)
    assert len(rendered.encode("utf-8")) <= TOTAL_BUDGET_CHARS
    reference = re.search(r"bridge_memory:turns:recovery:([0-9]+):([0-9]+)", rendered)
    assert reference
    database = tmp_path / "memory.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE turns (id INTEGER, role TEXT, source TEXT, text TEXT)")
        conn.executemany("INSERT INTO turns VALUES (?, ?, ?, ?)",
                         [(row["id"], row["role"], row["source"], row["text"]) for row in rows])
        originals = conn.execute("SELECT id, text FROM turns WHERE role=? AND id BETWEEN ? AND ?",
                                 ("recovery", int(reference[1]), int(reference[2]))).fetchall()
    assert originals and all(json.loads(content)["summary"].startswith("record-") for _, content in originals)
    # The same memory row must not be re-injected as both old and recent.
    assert rendered.count("turn=30 | source=") <= 1


@pytest.mark.asyncio
async def test_real_provider_error_remains_typed_after_capacity_preparation(tmp_path):
    import httpx
    from tests.test_openrouter_multimodal import _adapter

    adapter = _adapter(tmp_path)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            400, json={"error": {"code": "context_length_exceeded", "message": "context too long"}},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        result = await adapter.generate_response("normal request", "typed-capacity")
    assert len(requests) == 1
    assert result.is_success is False
    assert result.error_code == "CONTEXT_CAPACITY_REJECTED"
