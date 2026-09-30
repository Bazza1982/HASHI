"""Phone progress and cancellation follow Run facts without a second model call."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from tests.test_live_voice_actions import action, action_rows, decision, event_details, speak

pytest_plugins = ("tests.test_live_voice_actions",)


async def _stop_auto_relays(phone):
    tasks = tuple(phone.manager._relay_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _set_run_state(phone, run_id, state):
    with phone.store._lock, phone.store._connection() as connection:
        connection.execute("UPDATE runs SET state=? WHERE run_id=?", (state, run_id))


@pytest.mark.asyncio
async def test_invalid_action_shape_is_repaired_once_before_gmail_request_is_lost(phone):
    phone.judgments = [
        {"route": "act", "complete": True, "reply": "", "actions": []},
        decision(action("query", "Run the requested Gmail check once")),
    ]
    await speak(phone, "Run Gmail and tell me the newest messages")
    assert len(phone.admit_calls) == 1
    assert len(action_rows(phone)) == 1
    assert len(event_details(phone, "voice.live.action.judgment_repair")) == 1
    assert not event_details(phone, "voice.live.action.judgment_rejected")


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_only_real_presentable_progress_is_spoken_when_call_prefers_it(phone, monkeypatch, enabled):
    phone.judgments = [{**decision(action("query", "Check the news")),
                        "progress_preference": "on" if enabled else "off"}]
    await speak(phone, "Check the news, and give me brief updates" if enabled else
                      "Check the news, no interim reminders")
    await _stop_auto_relays(phone)
    item = action_rows(phone)[0]
    _set_run_state(phone, item["run_id"], "running")
    tick = [1.0]
    monkeypatch.setattr("orchestrator.frontend_live_voice.manager.monotonic", lambda: tick[0])
    polled = asyncio.Event()

    async def poll(_binding, _request_id, after_sequence, _limit):
        tick[0] = 30.0
        polled.set()
        return {"ok": True, "ephemeral_epoch": "worker-1", "events": [
            {"sequence": 1, "presentation_channel": "thinking", "summary": "private reasoning",
             "presentation_enabled": True},
            {"sequence": 2, "delivery_class": "user_commentary",
             "presentation_channel": "commentary", "summary": "I found the first sources.",
             "presentation_enabled": False},
        ] if after_sequence == 0 else []}

    phone.manager._poll_run_activity = poll
    task = asyncio.create_task(phone.manager._relay_run(phone.binding, item["delegation_id"],
        item["run_id"], phone.store.get_run(item["run_id"])["request_id"]))
    try:
        await asyncio.wait_for(polled.wait(), timeout=2)
        await asyncio.sleep(0.03)
        spoken = [payload["content"] for _binding, payload in phone.updates
                  if payload["kind"] == "commentary"]
        if enabled:
            assert len(spoken) == 2
            assert spoken[-1] == "I found the first sources."
            assert "private reasoning" not in " ".join(spoken)
        else:
            assert spoken == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_running_stop_is_confirmed_when_run_stops_even_with_partial_reads(phone):
    phone.judgments = [decision(action("query", "Find all current news"))]
    await speak(phone, "Find all current news")
    await _stop_auto_relays(phone)
    item = action_rows(phone)[0]
    _set_run_state(phone, item["run_id"], "running")
    phone.manager._cancel_action_run = AsyncMock(return_value={
        "interrupted": True, "cancelled_before_start": False,
        "evidence_ref": "request:sample:interrupted-effects-unconfirmed"})
    pending = await phone.manager._cancel_existing_action(phone.binding, item)
    assert pending["status"] == "running"
    assert len(event_details(phone, "voice.live.action.stop_requested")) == 1
    second = await phone.manager._cancel_existing_action(phone.binding, item)
    assert second["status"] == "running"
    assert phone.manager._cancel_action_run.await_count == 1
    assert len(event_details(phone, "voice.live.action.stop_requested")) == 1
    _set_run_state(phone, item["run_id"], "stopped")
    phone.manager._inspect_action_results = AsyncMock(return_value={
        "actions": [{"action_id": item["action_id"], "status": "verified",
                     "evidence_refs": ["web:partial"], "receipt": "Some sources were read."}],
        "tool_observations": [
            {"action_id": item["action_id"], "kind": "read", "evidence_ref": "web:one"},
            {"action_id": item["action_id"], "kind": "read", "evidence_ref": "web:two"}],
    })
    await phone.manager._relay_run(phone.binding, item["delegation_id"], item["run_id"],
        phone.store.get_run(item["run_id"])["request_id"])
    assert action_rows(phone)[0]["status"] == "cancelled"
    assert len(event_details(phone, "voice.live.action.stop_confirmed")) == 1
    assert "stopped" in action_rows(phone)[0]["receipt"].lower()
    assert "2 recorded reads" in action_rows(phone)[0]["receipt"]


@pytest.mark.asyncio
async def test_stop_intent_survives_terminal_race_before_worker_ack(phone):
    phone.judgments = [decision(action("query", "Check the news"))]
    await speak(phone, "Check the news")
    await _stop_auto_relays(phone)
    item = action_rows(phone)[0]
    _set_run_state(phone, item["run_id"], "running")

    async def stop_after_run_finishes(_binding, _run_id):
        _set_run_state(phone, item["run_id"], "stopped")
        await phone.manager._relay_run(phone.binding, item["delegation_id"], item["run_id"],
            phone.store.get_run(item["run_id"])["request_id"])
        return {"interrupted": True, "evidence_ref": "request:interrupted"}

    phone.manager._cancel_action_run = stop_after_run_finishes
    result = await phone.manager._cancel_existing_action(phone.binding, item)
    assert result["status"] == "cancelled"
    assert action_rows(phone)[0]["status"] == "cancelled"
    assert len(event_details(phone, "voice.live.action.stop_intended")) == 1
    assert len(event_details(phone, "voice.live.action.stop_confirmed")) == 1


@pytest.mark.asyncio
async def test_unconfirmed_stop_does_not_freeze_running_progress(phone):
    phone.judgments = [decision(action("query", "Check the news"))]
    await speak(phone, "Check the news")
    await _stop_auto_relays(phone)
    item = action_rows(phone)[0]
    _set_run_state(phone, item["run_id"], "running")
    phone.manager._cancel_action_run = AsyncMock(side_effect=RuntimeError("worker unavailable"))
    result = await phone.manager._cancel_existing_action(phone.binding, item)
    assert result["status"] == "unknown"
    assert not phone.manager._stop_was_requested(phone.binding, item["action_id"])
    assert len(event_details(phone, "voice.live.action.stop_unconfirmed")) == 1

    phone.manager._cancel_action_run = AsyncMock(return_value={
        "interrupted": True, "evidence_ref": "request:second-stop:accepted"})
    phone.judgments = [decision(action("cancel", "Stop the same query", "cancel", item["action_id"]))]
    await speak(phone, "Stop the same query now", start=200, end=300, source="retry-stop")
    assert phone.manager._cancel_action_run.await_count == 1
    assert phone.manager._stop_was_requested(phone.binding, item["action_id"])
    assert action_rows(phone)[0]["status"] == "unknown"
    assert "stop" in action_rows(phone)[0]["receipt"].lower()


@pytest.mark.asyncio
async def test_lost_stop_ack_then_terminal_stopped_reports_fact_without_claiming_cause(phone):
    phone.judgments = [decision(action("query", "Check the news"))]
    await speak(phone, "Check the news")
    await _stop_auto_relays(phone)
    item = action_rows(phone)[0]
    _set_run_state(phone, item["run_id"], "running")
    phone.manager._cancel_action_run = AsyncMock(side_effect=TimeoutError("ack lost"))
    await phone.manager._cancel_existing_action(phone.binding, item)
    _set_run_state(phone, item["run_id"], "stopped")
    await phone.manager._relay_run(phone.binding, item["delegation_id"], item["run_id"],
        phone.store.get_run(item["run_id"])["request_id"])
    state = action_rows(phone)[0]
    assert state["status"] == "unknown"
    assert "now stopped" in state["receipt"]
    assert "cannot confirm whether" in state["receipt"]
    assert "no completed answer" in state["receipt"]
    assert not event_details(phone, "voice.live.action.stop_confirmed")


@pytest.mark.asyncio
async def test_failed_query_is_reported_as_ended_not_still_running(phone):
    phone.judgments = [decision(action("query", "Check the news"))]
    await speak(phone, "Check the news")
    await _stop_auto_relays(phone)
    item = action_rows(phone)[0]
    _set_run_state(phone, item["run_id"], "failed")
    await phone.manager._relay_run(phone.binding, item["delegation_id"], item["run_id"],
        phone.store.get_run(item["run_id"])["request_id"])
    state = action_rows(phone)[0]
    assert state["status"] == "failed"
    assert "ended with an error" in state["receipt"]
    assert "no completed answer" in state["receipt"]


@pytest.mark.asyncio
async def test_completed_write_before_stop_reports_both_stop_timing_and_saved_effect(phone):
    phone.judgments = [decision(action("write", "Save the exercise record"))]
    await speak(phone, "Save the exercise record")
    await _stop_auto_relays(phone)
    item = action_rows(phone)[0]
    phone.manager._action_event(phone.binding, "stop_requested", {
        "action_id": item["action_id"], "run_id": item["run_id"], "interrupt_sent": True})
    _set_run_state(phone, item["run_id"], "completed")
    phone.manager._inspect_action_results = AsyncMock(return_value={"actions": [{
        "action_id": item["action_id"], "status": "verified",
        "evidence_refs": ["file:readback"], "receipt": "Exercise saved and read back."}]})
    await phone.manager._relay_run(phone.binding, item["delegation_id"], item["run_id"],
        phone.store.get_run(item["run_id"])["request_id"])
    state = action_rows(phone)[0]
    assert state["status"] == "verified"
    assert "before it could be stopped" in state["receipt"]
    assert "saved and read back" in state["receipt"]


@pytest.mark.asyncio
async def test_late_stop_response_cannot_downgrade_verified_effect(phone):
    phone.judgments = [decision(action("write", "Save the exercise record"))]
    await speak(phone, "Save the exercise record")
    await _stop_auto_relays(phone)
    item = action_rows(phone)[0]

    async def effect_finishes_during_stop(_binding, _run_id):
        phone.manager.actions.transition(phone.binding, item["action_id"], "verified",
            evidence_refs=["file:readback"], receipt="The record was saved and read back.")
        return {"interrupted": False}

    phone.manager._cancel_action_run = effect_finishes_during_stop
    result = await phone.manager._cancel_existing_action(phone.binding, item)
    assert result["status"] == "verified"
    assert action_rows(phone)[0]["status"] == "verified"
    assert action_rows(phone)[0]["receipt"] == "The record was saved and read back."


@pytest.mark.asyncio
async def test_recovered_relay_does_not_repeat_observed_progress(phone):
    phone.judgments = [decision(action("query", "Check the news"))]
    await speak(phone, "Check the news")
    await _stop_auto_relays(phone)
    item = action_rows(phone)[0]
    _set_run_state(phone, item["run_id"], "running")
    phone.manager._action_event(phone.binding, "progress_started", {"run_id": item["run_id"]})
    phone.manager._action_event(phone.binding, "progress_observed", {
        "run_id": item["run_id"], "source_epoch": "old-worker", "source_sequence": 2})
    polled = asyncio.Event()
    cursors = []

    async def poll(_binding, _request_id, after_sequence, _limit):
        cursors.append(after_sequence)
        polled.set()
        return {"ok": True, "ephemeral_epoch": "new-worker", "latest_sequence": 2,
                "events": [{"sequence": 2, "presentation_channel": "commentary",
                            "presentation_enabled": True, "summary": "Old progress"}]}

    phone.manager._poll_run_activity = poll
    task = asyncio.create_task(phone.manager._relay_run(phone.binding, item["delegation_id"],
        item["run_id"], phone.store.get_run(item["run_id"])["request_id"]))
    try:
        await asyncio.wait_for(polled.wait(), timeout=2)
        await asyncio.sleep(0.03)
        assert cursors == [2]
        assert phone.updates == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_repeat_all_reuses_complete_canonical_query_report_without_new_run(phone):
    phone.judgments = [decision(action("query", "Find all current news"))]
    await speak(phone, "Find all current news")
    await _stop_auto_relays(phone)
    item = action_rows(phone)[0]
    run = phone.store.get_run(item["run_id"])
    report = "\n".join(f"News {number}: full detail" for number in range(1, 24))
    phone.store.finish_request(run["request_id"], success=True, assistant_text=report)
    phone.manager.actions.transition(phone.binding, item["action_id"], "unknown",
        receipt="The sources were not checked item by item.")
    state = phone.manager._semantic_context(phone.binding,
        type("Proposal", (), {"text": "Explain the fireworks item"})())
    assert state["actions"][0]["run_state"] == "completed"
    assert state["actions"][0]["answer_available"] is True
    assert state["actions"][0]["effect_status"] == "unknown"
    phone.judgments = [decision(action("query", "Tell me every finding", "reuse", item["action_id"]))]
    await speak(phone, "Tell me all the news you just found", start=200, end=300, source="repeat-all")
    assert len(phone.admit_calls) == 1
    assert len(action_rows(phone)) == 1
    assert phone.updates[-1][1]["kind"] == "commentary"
    assert report in phone.updates[-1][1]["content"]
    assert "News 23" in phone.updates[-1][1]["content"]


@pytest.mark.asyncio
async def test_backend_route_fact_reaches_foreground_without_spoken_progress(phone):
    phone.judgments = [decision(action("query", "Check the news"))]
    await speak(phone, "Check the news")
    await _stop_auto_relays(phone)
    item = action_rows(phone)[0]
    _set_run_state(phone, item["run_id"], "running")
    polled = asyncio.Event()

    async def poll(_binding, _request_id, after_sequence, _limit):
        polled.set()
        return {"ok": True, "ephemeral_epoch": "worker-1", "events": [
            {"sequence": 1, "kind": "model_route", "engine": "her-v3",
             "model_provider": "deepseek-api", "model": "deepseek-flash",
             "route_status": "selected", "attempt": 1,
             "presentation_channel": "internal", "presentation_enabled": False},
            {"sequence": 2, "kind": "model_route", "engine": "her-v3",
             "model_provider": "deepseek-api", "model": "deepseek-flash",
             "route_status": "returned", "attempt": 1,
             "presentation_channel": "internal", "presentation_enabled": False},
        ] if after_sequence == 0 else []}

    phone.manager._poll_run_activity = poll
    task = asyncio.create_task(phone.manager._relay_run(phone.binding, item["delegation_id"],
        item["run_id"], phone.store.get_run(item["run_id"])["request_id"]))
    try:
        await asyncio.wait_for(polled.wait(), timeout=2)
        await asyncio.sleep(0.03)
        assert [payload["kind"] for _binding, payload in phone.updates] == [
            "commentary", "thinking", "thinking"]
        assert "deepseek-flash" not in phone.updates[0][1]["content"]
        routes = event_details(phone, "voice.live.action.model_route")
        assert [route["route_status"] for route in routes] == ["selected", "returned"]
        state = phone.manager._semantic_context(phone.binding,
            type("Proposal", (), {"text": "Which model is working?"})())
        assert state["actions"][0]["model_route"]["route_status"] == "returned"
        assert state["actions"][0]["model_route"]["model"] == "deepseek-flash"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_fast_completed_run_still_hands_model_route_to_foreground(phone):
    phone.judgments = [decision(action("query", "Check the news"))]
    await speak(phone, "Check the news")
    await _stop_auto_relays(phone)
    item = action_rows(phone)[0]
    run = phone.store.get_run(item["run_id"])
    phone.store.finish_request(run["request_id"], success=True,
                               assistant_text="The checked headlines are ready.")

    async def poll(_binding, _request_id, after_sequence, _limit):
        if after_sequence == 0:
            return {"ok": True, "ephemeral_epoch": "fast-worker", "latest_sequence": 66,
                    "events": [{"sequence": number, "kind": "tool_delta",
                                "presentation_channel": "verbose", "presentation_enabled": False}
                               for number in range(1, 65)]}
        return {"ok": True, "ephemeral_epoch": "fast-worker", "latest_sequence": 66, "events": [
            {"sequence": 65, "kind": "model_route", "engine": "her-v3",
             "model_provider": "deepseek-api", "model": "deepseek-flash",
             "route_status": "returned", "attempt": 1,
             "presentation_channel": "internal", "presentation_enabled": False},
            {"sequence": 66, "kind": "user_commentary", "delivery_class": "user_commentary",
             "presentation_channel": "commentary", "presentation_enabled": True,
             "summary": "An old interim update must not play after completion."},
        ]}

    phone.manager._poll_run_activity = poll
    await phone.manager._relay_run(phone.binding, item["delegation_id"], item["run_id"], run["request_id"])
    routes = event_details(phone, "voice.live.action.model_route")
    assert len(routes) == 1 and routes[0]["route_status"] == "returned"
    assert any(payload["kind"] == "thinking" for _binding, payload in phone.updates)
    assert not any("old interim" in payload["content"] for _binding, payload in phone.updates)
