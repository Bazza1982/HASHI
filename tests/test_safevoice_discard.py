"""Safe Voice discard is an intentional, durable Run termination."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.base import BackendResponse
from orchestrator import runtime_lifecycle, runtime_pipeline, runtime_session
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.function_worker_host import FunctionWorkerHost
from orchestrator.frontend_projection import project_frontend_event
from orchestrator.session_store import SessionConflict, StaleFencingToken
from orchestrator.workbench_api import WorkbenchApiServer
from tests.test_audit_run_handoff import _listener_runtime
from tests.test_session_attachment_authorization import fixture, transcript_fixture
from tests.test_wrapper_commands import (
    _completed_task, _make_background_runtime, _make_foreground_runtime, _queued_request,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_proxy", [False, True])
async def test_discarded_codex_voice_finishes_canonical_run_without_error(tmp_path, monkeypatch, worker_proxy):
    mgr, backend, store, session, run, meta, content = fixture(tmp_path, codex=True)
    record, state = transcript_fixture(mgr, store, run, content, status="ready", safe=True)
    challenged = asyncio.Event()

    async def challenge():
        store.require_voice_transcript_confirmation(request_id=run["request_id"])
        state["status"] = "pending_confirmation"
        challenged.set()

    state["request_confirmation"] = challenge
    state["transcript_decisions"] = {record["transcript_id"]: "pending_confirmation"}
    generation = asyncio.create_task(
        mgr.generate_response("synthetic", run["request_id"], request_content=content)
    )
    await asyncio.wait_for(challenged.wait(), 3)
    assert not hasattr(backend, "seen_prompt")

    decision = dict(
        session_id=session["session_id"],
        owner_id=session["owner_id"],
        transcript_id=record["transcript_id"],
        expected_context_generation=session["context_generation"],
    )
    api_runtime = mgr.runtime
    if worker_proxy:
        host = SimpleNamespace(_require_runtime=lambda: mgr.runtime)

        async def remote_decider(**params):
            return FunctionWorkerHost._decide_native_transcript(host, params)

        api_runtime = SimpleNamespace(is_function_worker_proxy=True,
                                      decide_native_voice_transcript=remote_decider)
    api = SimpleNamespace(
        session_store=store, _v1_owner_id=lambda _request: session["owner_id"],
        _native_audio_chat_v1_ready=lambda: True, _runtime_map=lambda: {"qa": api_runtime},
    )

    class Request:
        match_info = {"session_id": session["session_id"],
                      "transcript_id": record["transcript_id"]}

        async def json(self):
            return {"decision": "discard", "session_context_generation": session["context_generation"]}

    result = await WorkbenchApiServer.handle_v1_voice_transcript_decide(api, Request())
    assert result.status == 200
    result = await WorkbenchApiServer.handle_v1_voice_transcript_decide(api, Request())
    assert result.status == 200
    with pytest.raises(SessionConflict):
        store.decide_voice_transcript_by_id(**decision, confirmed=True)
    response = await asyncio.wait_for(generation, 3)
    assert runtime_pipeline.is_safe_voice_discard(response)
    assert not hasattr(backend, "seen_prompt")

    runtime = _listener_runtime(
        store, owner=session["owner_id"], agent="qa", request_id=run["request_id"],
        fencing_token=meta["hashi_fencing_token"],
    )
    runtime.config.active_backend = "codex-cli"
    runtime._native_voice_transcripts = mgr.runtime._native_voice_transcripts
    runtime._log_maintenance = lambda *args, **kwargs: None
    observed = []
    runtime._request_listeners[run["request_id"]] = [observed.append]
    monkeypatch.setattr(runtime_session, "capture_backend_binding", lambda *_a, **_k: None)
    monkeypatch.setattr("orchestrator.frontend_whatsapp_mirror.deliver_whatsapp_mirror", AsyncMock())
    monkeypatch.setattr("orchestrator.runtime_debug_reporting.schedule_terminal_diagnostic", lambda *_a, **_k: None)
    failure_report = []
    monkeypatch.setattr("orchestrator.runtime_debug_reporting.schedule_failure_report",
                        lambda *_a, **_k: failure_report.append(True))
    monkeypatch.setattr("orchestrator.terminal_console.finish_request", lambda *_a, **_k: None)
    item = SimpleNamespace(request_id=run["request_id"], source="session-api", summary="voice")
    await runtime_pipeline.handle_safe_voice_discard(runtime, item)

    terminal = store.get_run(run["run_id"])
    assert terminal["state"] == "stopped"
    assert terminal["error_code"] is None and terminal["error_text"] is None
    assert terminal["final_message_id"] is None
    assert observed[0]["interrupt_reason"] == "safe_voice_discarded"
    assert "error" not in observed[0] and "error_code" not in observed[0]
    assert not failure_report
    activity = runtime.request_activity.poll(run["request_id"])
    assert activity["state"] == "interrupted"
    assert not any(event["kind"] == "error" for event in activity["events"])

    events = store.events(session["session_id"], owner_id=session["owner_id"], run_id=run["run_id"])
    assert sum(event["kind"] == "voice.input.transcript_discarded" for event in events) == 1
    terminals = [event for event in events if event["phase"] == "terminal"]
    assert len(terminals) == 1 and terminals[0]["kind"] == "run.stopped"
    assert terminals[0]["detail"] == {"reason": "safe_voice_discarded"}
    projected = project_frontend_event(terminals[0])
    assert projected["semantic_kind"] == "status"
    assert "public_failure" not in projected and "error_code" not in projected
    user_message = store.get_message(run["user_message_id"], session_id=session["session_id"],
                                     owner_id=session["owner_id"])
    assert "SYNTHETIC_VOICE_BLUE" not in str(user_message["content"])
    replay = runtime_session.finish_request_from_listener(
        runtime, run["request_id"], {"success": False, "error": "late failure"}
    )
    assert replay.payload["interrupt_reason"] == "safe_voice_discarded"
    assert "error" not in replay.payload

    next_run = store.accept_run(
        session_id=session["session_id"], owner_id=session["owner_id"], agent_id="qa",
        request_id="req-next-after-discard", text="next", source="session-api",
        idempotency_key="next-after-discard",
    )
    token = store.mark_request_running(next_run.request_id, worker_id="qa-worker")
    assert token
    assert store.finish_request(next_run.request_id, success=True, assistant_text="ok",
                                fencing_token=token)["state"] == "completed"


def test_discard_terminal_requires_durable_decision_and_current_fence(tmp_path):
    mgr, backend, store, session, run, meta, content = fixture(tmp_path, codex=True)
    transcript_fixture(mgr, store, run, content, status="ready", safe=True)
    kwargs = dict(success=False, failure_state="stopped", termination_reason="safe_voice_discarded")
    with pytest.raises(SessionConflict):
        store.finish_request(run["request_id"], fencing_token=meta["hashi_fencing_token"], **kwargs)
    assert store.get_run(run["run_id"])["state"] == "running"
    store.require_voice_transcript_confirmation(request_id=run["request_id"])
    store.decide_voice_transcript(request_id=run["request_id"], confirmed=False)
    with pytest.raises(StaleFencingToken):
        store.finish_request(run["request_id"], fencing_token=meta["hashi_fencing_token"] + 1, **kwargs)
    assert store.get_run(run["run_id"])["state"] == "running"
    assert store.finish_request(run["request_id"], fencing_token=meta["hashi_fencing_token"],
                                **kwargs)["state"] == "stopped"


def test_prior_user_stop_wins_over_late_discard(tmp_path, monkeypatch):
    mgr, backend, store, session, run, meta, content = fixture(tmp_path, codex=True)
    transcript_fixture(mgr, store, run, content, status="ready", safe=True)
    store.require_voice_transcript_confirmation(request_id=run["request_id"])
    store.decide_voice_transcript(request_id=run["request_id"], confirmed=False)
    store.finish_request(run["request_id"], success=False, failure_state="stopped",
                         error_text="Interrupted by user_stop", fencing_token=meta["hashi_fencing_token"])
    runtime = _listener_runtime(store, owner=session["owner_id"], agent="qa",
                                request_id=run["request_id"], fencing_token=meta["hashi_fencing_token"])
    monkeypatch.setattr(runtime_session, "capture_backend_binding", lambda *_a, **_k: None)
    resolved = runtime_session.finish_request_from_listener(
        runtime, run["request_id"],
        {"success": False, "interrupted": True, "interrupt_reason": "safe_voice_discarded"},
    )
    assert resolved.payload["interrupt_reason"] == "user_stop"
    assert resolved.run["error_text"] == "Interrupted by user_stop"


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["discard", "confirm"])
async def test_multiple_voice_inputs_require_all_confirmations(tmp_path, decision):
    mgr, backend, store, session, run, _meta, content = fixture(
        tmp_path, codex=True, voice_count=2
    )
    records = [
        store.record_voice_transcript(
            request_id=run["request_id"], attachment_id=part["attachment_id"],
            text=f"VOICE_{index}", provenance="local_stt", safe_voice_state="pending_confirmation",
        )
        for index, part in enumerate(content["parts"])
    ]
    state = {
        "request_id": run["request_id"],
        "attachment_ids": [part["attachment_id"] for part in content["parts"]],
        "status": "pending_confirmation", "safe_voice": True,
        "ready_event": asyncio.Event(), "release_event": asyncio.Event(),
        "gate_lock": asyncio.Lock(),
        "transcript_decisions": {row["transcript_id"]: "pending_confirmation" for row in records},
    }
    state["ready_event"].set()
    mgr.runtime._native_voice_transcripts = {run["request_id"]: state}
    mgr.runtime._native_voice_transcripts.update({part["attachment_id"]: state for part in content["parts"]})
    api = SimpleNamespace(
        session_store=store, _v1_owner_id=lambda _request: session["owner_id"],
        _native_audio_chat_v1_ready=lambda: True, _runtime_map=lambda: {"qa": mgr.runtime},
    )

    class Request:
        def __init__(self, transcript_id):
            self.match_info = {"session_id": session["session_id"], "transcript_id": transcript_id}

        async def json(self):
            return {"decision": decision, "session_context_generation": session["context_generation"]}

    task = asyncio.create_task(mgr.generate_response("synthetic", run["request_id"], request_content=content))
    await asyncio.sleep(0)
    assert (await WorkbenchApiServer.handle_v1_voice_transcript_decide(
        api, Request(records[0]["transcript_id"])
    )).status == 200
    if decision == "confirm":
        assert not task.done()
        assert not hasattr(backend, "seen_prompt")
        assert (await WorkbenchApiServer.handle_v1_voice_transcript_decide(
            api, Request(records[1]["transcript_id"])
        )).status == 200
    response = await asyncio.wait_for(task, 3)
    if decision == "discard":
        assert runtime_pipeline.is_safe_voice_discard(response)
        assert not hasattr(backend, "seen_prompt")
    else:
        assert response.is_success
        assert "VOICE_0" in backend.seen_prompt and "VOICE_1" in backend.seen_prompt


@pytest.mark.asyncio
async def test_discard_is_not_accepted_from_memory_without_durable_decision(tmp_path):
    mgr, backend, store, session, run, meta, content = fixture(tmp_path, codex=True)
    _record, state = transcript_fixture(mgr, store, run, content, status="ready", safe=True)
    state["status"] = "discarded"
    state["release_event"].set()
    response = await mgr.generate_response("synthetic", run["request_id"], request_content=content)
    assert not response.is_success and response.error_code == "MEDIA_FALLBACK_UNAVAILABLE"
    assert not hasattr(backend, "seen_prompt")


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_foreground_and_background_discard_do_not_send_backend_error(tmp_path, background, monkeypatch):
    response = BackendResponse(text="", duration_ms=0, is_success=False,
                               stop_reason="safe_voice_discarded")
    if background:
        runtime, sent, _voices = _make_background_runtime(tmp_path)
        runtime._log_maintenance = lambda *_a, **_k: None
    else:
        runtime, sent, _voices, _hchat = _make_foreground_runtime(tmp_path)
        runtime.backend_manager.generate_response = lambda *_a, **_k: _completed_task(response)
    errors = []
    runtime._mark_error = errors.append
    receipts = []
    monkeypatch.setattr("orchestrator.runtime_cross_session.record_turn_result",
                        lambda *_a, **_k: receipts.append(True))
    observed = []
    runtime.register_request_listener("req-001", observed.append)
    item = _queued_request()
    item.source = "session-api"

    if background:
        task = asyncio.create_task(_completed_task(response))
        await task
        await FlexibleAgentRuntime._on_background_complete(runtime, task, item)
    else:
        await runtime.queue.put(item)
        task = asyncio.create_task(runtime_lifecycle.process_queue(runtime))
        try:
            await asyncio.wait_for(runtime.queue.join(), 3)
        finally:
            task.cancel()
            await task

    assert observed and observed[0]["interrupt_reason"] == "safe_voice_discarded"
    assert not observed[0].get("error")
    assert not errors and not sent
    assert not receipts
