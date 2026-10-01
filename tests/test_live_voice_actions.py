"""Phone intent/action boundaries with real temporary file effects and durable receipts."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from tests import test_live_voice_integration as integration
from orchestrator.frontend_live_voice.actions import effect_evidence
from orchestrator.frontend_live_voice.delegation_policy import ActionIntent, parse_decision
from orchestrator.frontend_live_voice.protocol import Fragment
from orchestrator.voice_result_outline import numbered_source_outline
from orchestrator.frontend_live_voice import worker_actions
from orchestrator import runtime_session
from tools.registry import ToolRegistry


def actual_admission_api(phone, tmp_path):
    """Real API -> Worker dispatch -> runtime PAO -> persistent Run and queue."""
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.function_worker_host import FunctionWorkerHost
    from orchestrator.function_worker_supervisor import AgentRuntimeHandle
    from orchestrator.function_worker_protocol import FunctionWorkerRemoteError
    from orchestrator.workbench_api import WorkbenchApiServer

    workspace = tmp_path / "agent"
    workspace.mkdir()
    sequence = iter(range(1, 20))
    runtime = SimpleNamespace(name=phone.agent_id, session_store=phone.store,
        workspace_dir=workspace, global_config=SimpleNamespace(
            authorized_id=7, instance_id="HASHI", bridge_home=tmp_path, project_root=None),
        config=SimpleNamespace(workspace_dir=workspace), queue=asyncio.Queue(),
        error_logger=logging.getLogger("test.phone.error"), message_logger=logging.getLogger("test.phone.messages"),
        request_activity=SimpleNamespace(start=lambda *args, **kwargs: None),
        next_request_id=lambda: f"actual-admission-{next(sequence)}")
    runtime.enqueue_request = MethodType(FlexibleAgentRuntime.enqueue_request, runtime)
    host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    host.runtime, host.phase, host.accepting, host.agent_name = runtime, "ACTIVE", True, phone.agent_id
    host.emit_metadata = AsyncMock()
    handle = AgentRuntimeHandle.__new__(AgentRuntimeHandle)
    handle.metadata = {"primary_chat_id": 7}
    handle._outstanding_request_ids = set()

    async def route(_self, method, params):
        # Exercise the real RPC handlers and serialization boundary; no process,
        # model, Telegram connection or production Agent starts in this test.
        try:
            return await host.handle_request(method, json.loads(json.dumps(params)))
        except Exception as exc:
            raise FunctionWorkerRemoteError(method, {"type": type(exc).__name__, "message": str(exc)}) from exc

    handle._route = MethodType(route, handle)
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    server.session_store = phone.store
    server._runtime_map = lambda: {phone.agent_id: handle}
    server.global_config = runtime.global_config
    return server, runtime


@pytest.mark.asyncio
async def test_real_worker_admission_is_scoped_durable_and_idempotent(phone, tmp_path):
    server, runtime = actual_admission_api(phone, tmp_path)
    phone.store.bind_primary_session(owner_id=phone.owner_id, agent_id=phone.agent_id, session_id=phone.session_id)
    phone.manager._admit_run = server._admit_live_voice_run
    phone.judgments = [decision(action("write", "Create fitness.txt containing Exercise 40 minutes, then read it back"))]
    await speak(phone, "Create fitness.txt, save Exercise 40 minutes and read the saved file back")
    rows = action_rows(phone)
    assert len(rows) == 1 and rows[0]["run_id"] is not None
    queued = runtime.queue.get_nowait()
    assert queued.run_id == rows[0]["run_id"]
    assert "read it back" in queued.prompt
    run = phone.store.get_run(queued.run_id, owner_id=phone.owner_id)
    assert run["message_context"]["live_voice"]["call_id"] == phone.call_id
    with phone.store._lock, phone.store._connection() as connection:
        child = connection.execute("SELECT * FROM live_delegations WHERE call_id=? AND delegation_id=?",
            (phone.call_id, rows[0]["delegation_id"])).fetchone()
    proposal = await phone.manager.read_proposal(phone.binding, child["delegation_id"])
    same = await server._admit_live_voice_run(phone.binding, proposal, "live-delegation-" + child["decision_key"])
    assert same["run_id"] == queued.run_id
    assert runtime.queue.empty()
    assert len(phone.store.recent_session_runs(phone.session_id, owner_id=phone.owner_id)) == 1


@pytest.mark.asyncio
async def test_real_worker_preserves_order_of_independent_goals(phone, tmp_path):
    server, runtime = actual_admission_api(phone, tmp_path)
    phone.store.bind_primary_session(owner_id=phone.owner_id, agent_id=phone.agent_id, session_id=phone.session_id)
    phone.manager._admit_run = server._admit_live_voice_run
    requests = ["Check the mail delivery status", "Record exercise in fitness.txt and read it back"]
    phone.judgments = [decision(action("query", requests[0]), action("write", requests[1]))]
    await speak(phone, "Check mail and separately record exercise")
    queued = [runtime.queue.get_nowait(), runtime.queue.get_nowait()]
    assert [item.request_metadata["live_voice"]["delegation_id"] for item in queued] == [
        row["delegation_id"] for row in action_rows(phone)]
    for index, item in enumerate(queued):
        resolved = json.loads(item.prompt.split("\n", 1)[1])["resolved_actions"]
        assert [task["request"] for task in resolved] == [requests[index]]
    assert runtime.queue.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"owner_id": "foreign-owner"}, {"agent_id": "foreign-agent"}, {"context_generation": 2}])
async def test_real_admission_rejects_owner_agent_or_generation_change(phone, tmp_path, change):
    from orchestrator.frontend_live_voice.protocol import LiveVoiceError
    server, runtime = actual_admission_api(phone, tmp_path)
    phone.store.bind_primary_session(owner_id=phone.owner_id, agent_id=phone.agent_id, session_id=phone.session_id)
    proposal = SimpleNamespace(text="Synthetic only", execution_text="")
    with pytest.raises(LiveVoiceError, match="live_admission_scope_changed"):
        await server._admit_live_voice_run(replace(phone.binding, **change), proposal, "foreign-scope")
    assert runtime.queue.empty()
    assert phone.store.recent_session_runs(phone.session_id, owner_id=phone.owner_id) == []


@pytest.mark.asyncio
async def test_non_primary_scope_is_rejected_before_phone_context_or_worker_admission(phone, tmp_path):
    from orchestrator.frontend_live_voice.protocol import LiveVoiceError
    server, runtime = actual_admission_api(phone, tmp_path)
    primary = phone.store.ensure_default_session(owner_id=phone.owner_id, agent_id=phone.agent_id)
    phone.store.bind_primary_session(owner_id=phone.owner_id, agent_id=phone.agent_id, session_id=primary["session_id"])
    with pytest.raises(LiveVoiceError, match="live_scope_changed"):
        server._resolve_live_voice_phone_session(phone.agent_id, owner_id=phone.owner_id,
            session_id=phone.session_id, context_generation=1)
    proposal = SimpleNamespace(text="Save synthetic fitness data", execution_text="", delegation_id="target-request",
                               version=1, digest="a" * 64)
    with pytest.raises(LiveVoiceError, match="live_admission_scope_changed"):
        await server._admit_live_voice_run(phone.binding, proposal, "wrong-scope")
    assert runtime.queue.empty()
    assert phone.store.recent_session_runs(phone.session_id, owner_id=phone.owner_id) == []


@pytest.mark.asyncio
async def test_known_initial_rejection_closes_pending_receipt_without_recovery_retry(phone, tmp_path):
    server, runtime = actual_admission_api(phone, tmp_path)
    primary = phone.store.ensure_default_session(owner_id=phone.owner_id, agent_id=phone.agent_id)
    phone.store.bind_primary_session(owner_id=phone.owner_id, agent_id=phone.agent_id, session_id=primary["session_id"])
    phone.manager._admit_run = server._admit_live_voice_run
    phone.judgments = [decision(action("write", "Save synthetic exercise data"))]
    await speak(phone, "Save synthetic exercise data")
    assert action_rows(phone)[0]["status"] == "failed"
    with phone.store._lock, phone.store._connection() as connection:
        pending = connection.execute("SELECT COUNT(*) FROM live_control_receipts WHERE call_id=? AND operation='admission_pending'",
                                     (phone.call_id,)).fetchone()[0]
    assert pending == 0
    assert event_details(phone, "voice.live.action.admission_rejected")[0]["worker_invoked"] is False
    phone.manager._recover_action_relays(call_id=phone.call_id)
    assert runtime.queue.empty()
    assert phone.store.recent_session_runs(phone.session_id, owner_id=phone.owner_id) == []


@pytest.mark.asyncio
async def test_similar_exception_text_is_unknown_and_preserves_bounded_diagnostics(phone, tmp_path):
    server, runtime = actual_admission_api(phone, tmp_path)
    phone.store.bind_primary_session(owner_id=phone.owner_id, agent_id=phone.agent_id, session_id=phone.session_id)
    async def uncertain(*args, **kwargs):
        raise RuntimeError("live_admission_scope_changed secret caller words")
    runtime.enqueue_request = uncertain
    phone.manager._admit_run = server._admit_live_voice_run
    phone.judgments = [decision(action("write", "Save synthetic exercise data"))]
    await speak(phone, "Save synthetic exercise data")
    assert action_rows(phone)[0]["status"] == "unknown"
    assert not event_details(phone, "voice.live.action.admission_rejected")
    evidence = event_details(phone, "voice.live.action.admission_unconfirmed")[0]
    assert evidence["error_code"] == "live_outcome_unknown"
    assert evidence["exception_type"] == "RuntimeError"
    assert "secret caller words" not in json.dumps(evidence)
    audit = phone.manager.audit.path_for(phone.binding).read_text(encoding="utf-8")
    assert '"error_code":"live_outcome_unknown"' in audit
    assert action_rows(phone)[0]["action_id"] in audit
    assert "secret caller words" not in audit
    # A later scope failure cannot prove the earlier uncertain attempt had no
    # effect. Recovery retains its pending receipt instead of declaring failure.
    primary = phone.store.ensure_default_session(owner_id=phone.owner_id, agent_id=phone.agent_id)
    phone.store.bind_primary_session(owner_id=phone.owner_id, agent_id=phone.agent_id, session_id=primary["session_id"])
    with phone.store._lock, phone.store._connection() as connection:
        pending = connection.execute("SELECT * FROM live_control_receipts WHERE call_id=? AND operation='admission_pending'",
                                     (phone.call_id,)).fetchone()
    await phone.manager._resume_pending_admission(phone.binding, pending["idempotency_key"],
                                                  pending["request_digest"], pending["receipt_json"])
    assert action_rows(phone)[0]["status"] == "unknown"
    assert not event_details(phone, "voice.live.action.admission_rejected")
    with phone.store._lock, phone.store._connection() as connection:
        assert connection.execute("SELECT operation FROM live_control_receipts WHERE call_id=? AND idempotency_key=?",
                                  (phone.call_id, pending["idempotency_key"])).fetchone()[0] == "admission_pending"


def test_action_creation_preserves_requested_order_when_timestamps_match(phone, monkeypatch):
    from orchestrator.frontend_live_voice import actions as actions_module
    monkeypatch.setattr(actions_module, "stable_digest", lambda value: f"{value['index']:028d}")
    intents = [ActionIntent("query", "First independent goal"), ActionIntent("write", "Second independent goal")]
    created = phone.manager.actions.create(phone.binding, "ordered", intents)
    assert [row["request"] for row in created] == [item.request for item in intents]


def action(kind, request, relation="new", target=None):
    return {"kind": kind, "request": request, "relation": relation, "target_action_id": target}


def decision(*items):
    return {"route": "act", "complete": True, "reply": "", "actions": list(items)}


@pytest_asyncio.fixture
async def phone():
    harness = integration.LiveVoiceManagerStoreTests()
    harness.setUp()
    harness.updates = []
    harness.manager._action_reply_timeout_seconds = 0.02

    async def send(binding, **payload):
        harness.updates.append((binding, payload))
        return True

    harness.manager._send_provider_update = send
    try:
        yield harness
    finally:
        await harness.manager.shutdown()
        harness.tearDown()


async def speak(phone, text, *, start=0, end=100, source="speech"):
    await phone.manager.append_fragment_once(phone.binding, Fragment(source, "user", text, start, end))
    await phone.manager.register_delegation_once(phone.binding, source + "-proposal", end + 1)
    await phone.manager.schedule_proposal(phone.binding, source + "-proposal", end + 1)


def action_rows(phone):
    return phone.manager.actions.rows(phone.binding)


def event_details(phone, kind):
    with phone.store._lock, phone.store._connection() as connection:
        rows = connection.execute("SELECT detail_json FROM run_events WHERE session_id=? AND kind=? ORDER BY sequence",
                                  (phone.session_id, kind)).fetchall()
    return [json.loads(row[0]) for row in rows]


@pytest.mark.asyncio
async def test_no_provider_delegation_still_records_complete_speech(phone):
    phone.judgments = [decision(action("write", "Save a 30 minute exercise entry in the established fitness record"))]
    fragment = Fragment("just-speech", "user", "I exercised for thirty minutes. Record that.", 0, 100)
    await phone.manager.append_fragment_once(phone.binding, fragment)
    await phone.manager.note_user_fragment(phone.binding, fragment)
    await asyncio.sleep(1.1)
    assert len(phone.admit_calls) == 1
    assert len(action_rows(phone)) == 1
    assert "30 minute exercise" in phone.admitted_proposals[0].execution_text
    assert phone.admitted_proposals[0].text == fragment.text


@pytest.mark.asyncio
async def test_words_arriving_during_judgment_revoke_old_action_without_losing_first_fragment(phone):
    started, release = asyncio.Event(), asyncio.Event()
    heard = []

    async def judge(_binding, state):
        heard.append(state["utterance"])
        if len(heard) == 1:
            started.set()
            await release.wait()
            return decision(action("write", "Save the incomplete old record"))
        return decision(action("write", "Save the corrected forty minute exercise entry"))

    phone.manager._judge_action = judge
    first = Fragment("part-1", "user", "Record the exercise as ", 0, 100)
    await phone.manager.append_fragment_once(phone.binding, first)
    await phone.manager.note_user_fragment(phone.binding, first)
    await asyncio.wait_for(started.wait(), 2)
    second = Fragment("part-2", "user", "forty minutes, not thirty.", 101, 200)
    await phone.manager.append_fragment_once(phone.binding, second)
    await phone.manager.note_user_fragment(phone.binding, second)
    await asyncio.sleep(1.0)
    release.set()
    await asyncio.sleep(0.12)
    assert heard == [first.text, first.text + second.text]
    assert len(phone.admit_calls) == 1
    assert "forty minute" in phone.admitted_proposals[0].execution_text
    assert action_rows(phone)[0]["request"] != "Save the incomplete old record"


@pytest.mark.asyncio
async def test_independent_actions_have_separate_runs_and_scoped_cancel(phone):
    phone.judgments = [decision(action("query", "Check mail delivery"), action("write", "Record exercise"))]
    await speak(phone, "Check mail and record exercise")
    first, second = action_rows(phone)
    assert first["run_id"] != second["run_id"]
    assert len(phone.admit_calls) == 2
    cancelled = []

    async def cancel(_binding, run_id):
        cancelled.append(run_id)
        return {"cancelled_before_start": True, "evidence_ref": "request:removed-before-start"}

    phone.manager._cancel_action_run = cancel
    phone.judgments = [decision(action("cancel", "Cancel the mail check", "cancel", first["action_id"]))]
    await speak(phone, "Cancel only the mail check", start=200, end=300, source="cancel")
    rows = {row["action_id"]: row for row in action_rows(phone)}
    assert cancelled == [first["run_id"]]
    assert rows[first["action_id"]]["status"] == "cancelled"
    assert rows[second["action_id"]]["status"] == "accepted"
    assert rows[second["action_id"]]["run_id"] == second["run_id"]
    assert len(phone.admit_calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, task_text", [
    ("query", "Check current news"),
    ("execute", "Wait for ninety seconds"),
])
async def test_running_phone_stop_waits_for_terminal_confirmation(phone, monkeypatch, kind, task_text):
    phone.judgments = [decision(action(kind, task_text))]
    await speak(phone, task_text)
    original = action_rows(phone)[0]
    run = phone.store.get_run(original["run_id"], owner_id=phone.owner_id)
    phone.store.mark_request_running(run["request_id"], worker_id="worker")

    async def request_stop(_binding, run_id):
        assert run_id == original["run_id"]
        return {"interrupted": True,
                "evidence_ref": "request:" + run["request_id"] + ":interrupt-sent"}

    phone.manager._cancel_action_run = request_stop
    phone.judgments = [decision(action("cancel", "Stop that task", "cancel", original["action_id"]))]
    await speak(phone, "Stop that task and tell me what happened", start=200, end=300, source="stop")
    assert action_rows(phone)[0]["status"] == "running"
    assert event_details(phone, "voice.live.action.stop_requested")
    assert not event_details(phone, "voice.live.action.stop_confirmed")

    monkeypatch.setattr(runtime_session, "capture_backend_binding", lambda *args, **kwargs: None)
    runtime = SimpleNamespace(
        name=phone.agent_id, session_store=phone.store,
        config=SimpleNamespace(active_backend="her-v3"),
        backend_manager=SimpleNamespace(current_backend=None),
    )
    runtime_session.finish_request_from_listener(runtime, run["request_id"], {
        "success": False, "error": "Interrupted by user_stop",
        "interrupted": True, "interrupt_reason": "user_stop",
    })
    for _ in range(20):
        if event_details(phone, "voice.live.action.stop_confirmed"):
            break
        await asyncio.sleep(0.1)

    assert phone.store.get_run(original["run_id"], owner_id=phone.owner_id)["state"] == "stopped"
    assert event_details(phone, "voice.live.action.stop_confirmed")
    assert action_rows(phone)[0]["status"] == "cancelled"
    if kind == "execute":
        receipt = action_rows(phone)[0]["receipt"]
        assert phone.manager._action_text(phone.binding, "no_final_result") in receipt
        assert phone.manager._action_text(phone.binding, "execute_unconfirmed") in receipt
        assert phone.manager._action_text(phone.binding, "write_unconfirmed") not in receipt
    assert len(phone.admit_calls) == 1


@pytest.mark.asyncio
async def test_same_action_hurry_reuses_and_real_correction_updates_original(phone):
    phone.judgments = [decision(action("write", "Record exercise for 30 minutes"))]
    await speak(phone, "Record exercise for 30 minutes")
    original = action_rows(phone)[0]
    phone.manager.actions.transition(phone.binding, original["action_id"], "verified",
        evidence_refs=["tool:write:sha256:original"], receipt="Exercise saved for 30 minutes.")
    phone.judgments = [
        decision(action("write", "Report whether the exercise is saved", "reuse", original["action_id"])),
        decision(action("modify", "Change the same exercise entry from 30 to 40 minutes", "revise", original["action_id"])),
    ]
    await speak(phone, "Have you done it? Hurry", start=200, end=300, source="hurry")
    assert len(phone.admit_calls) == 1
    await speak(phone, "Change that to forty minutes", start=400, end=500, source="correct")
    assert len(phone.admit_calls) == 2
    latest = action_rows(phone)[-1]
    assert latest["target_action_id"] == original["action_id"]
    assert "original" in phone.admitted_proposals[-1].execution_text
    assert "30 to 40" in phone.admitted_proposals[-1].execution_text


@pytest.mark.asyncio
async def test_substantive_live_answer_does_not_trigger_second_reply(phone):
    await phone.manager.append_fragment_once(phone.binding, Fragment("already", "assistant", "The report contains no incidents.", 1, 20))
    phone.judgments = [{"route": "answer", "complete": True, "reply_needed": False, "reply": "", "actions": []}]
    await speak(phone, "Tell me that report", start=30, end=50)
    assert not phone.updates
    assert not phone.admit_calls


@pytest.mark.asyncio
async def test_same_agent_can_recall_complete_scheduled_report_without_new_run(phone):
    activity = phone.store.ensure_agent_activity_session(
        owner_id=phone.owner_id, agent_id=phone.agent_id,
    )
    accepted = phone.store.accept_run(
        session_id=activity["session_id"], owner_id=phone.owner_id,
        agent_id=phone.agent_id, request_id="scheduled-report", text="Make the report",
        source="scheduler", idempotency_key="scheduled-report",
    )
    phone.store.mark_request_running(accepted.request_id, worker_id="worker")
    report = "\n".join(f"News {item:02d}: complete detail" for item in range(1, 24))
    phone.store.finish_request(accepted.request_id, success=True, assistant_text=report)
    message_id = phone.store.get_run(accepted.run_id, owner_id=phone.owner_id)["final_message_id"]
    phone.judgments = [{"route": "recall", "complete": True, "reply_needed": False,
                        "reply": "", "actions": [], "result_ids": [message_id]}]

    await speak(phone, "Tell me all 23 saved news items")

    assert not phone.admit_calls
    assert not action_rows(phone)
    offered = "\n".join(payload["content"] for _, payload in phone.updates)
    assert all(f"News {item:02d}: complete detail" in offered for item in range(1, 24))
    assert event_details(phone, "voice.live.delegation.routed")[-1]["decision"] == "recalled"
    pending = event_details(phone, "voice.live.action.reply_pending")[-1]
    assert "News 23" not in pending["content"]
    assert pending["result_sources"] == [{"message_id": message_id, "start": 0}]
    assert phone.updates[0][1]["kind"] == "thinking"
    assert phone.updates[-1][1]["kind"] == "commentary"


@pytest.mark.asyncio
async def test_morning_question_receives_four_complete_saved_reports_without_new_work(phone):
    activity = phone.store.ensure_agent_activity_session(
        owner_id=phone.owner_id, agent_id=phone.agent_id,
    )
    reports = {
        "news": "Gemini; Micron; Russian energy strike",
        "gmail": "Gmail 36 messages and two decisions",
        "outlook": "Outlook 13 messages and three followups",
        "school": "Two students and 16 modules checked",
    }
    message_ids = []
    for name, conclusion in reports.items():
        accepted = phone.store.accept_run(
            session_id=activity["session_id"], owner_id=phone.owner_id,
            agent_id=phone.agent_id, request_id="morning-" + name,
            text="Prepare morning " + name, source="scheduler",
            idempotency_key="morning-" + name,
        )
        full_report = (name + " details. " * 55) + "\nConclusion: " + conclusion
        if name == "news":
            full_report = (
                "Today's two main themes are AI and storage.\n"
                "🔴 **今日重点**\n"
                "**1. Gemini 4 Argon** — programming model news.\n"
                "**2. Micron Q4** — storage cycle news.\n"
                "**3. Russian energy strike** — international news.\n"
                + full_report
            )
        assert len(full_report) > 360
        phone.store.finish_request(
            accepted.request_id, success=True, assistant_text=full_report,
        )
        message_ids.append(
            phone.store.get_run(accepted.run_id, owner_id=phone.owner_id)["final_message_id"]
        )
    phone.judgments = [{"route": "recall", "complete": True, "reply_needed": False,
                        "reply": "", "actions": [], "result_ids": message_ids}]

    await speak(phone, "Tell me the news, Gmail, Outlook and school results")

    assert not phone.admit_calls
    assert not action_rows(phone)
    offered = event_details(phone, "voice.live.action.reply_offered")[-1]
    assert [page["message_id"] for page in offered["result_pages"]] == message_ids
    staged = "\n".join(payload["content"] for _, payload in phone.updates
                       if payload["kind"] == "thinking")
    assert all(conclusion in staged for conclusion in reports.values())
    assert "Section 今日重点: 3 numbered entries." in staged
    assert "3. Russian energy strike" in staged
    assert event_details(phone, "voice.live.delegation.routed")[-1]["decision"] == "recalled"


def test_numbered_source_outline_distinguishes_intro_themes_from_report_items():
    original = (
        "Two main themes today: AI and storage.\n"
        "🔴 **今日重点**\n"
        "**1. Gemini** — first news item.\n"
        "**2. Micron** — second news item.\n"
        "**3. Russian energy strike** — third news item.\n"
        "🌏 **International**\n"
        "Other unnumbered news follows.\n"
    )
    outline = numbered_source_outline(original)
    assert "Section 今日重点: 3 numbered entries." in outline
    assert "3. Russian energy strike" in outline
    assert "International: " not in outline


def test_compact_source_outline_keeps_true_total_when_titles_do_not_fit():
    original = "# News\n" + "\n".join(
        f"{number}. Story {number} with source details and follow-up context"
        for number in range(1, 24)
    )
    outline = numbered_source_outline(original, max_chars=400)
    assert "Section News: 23 numbered entries." in outline
    assert "further numbered entries remain in the original" in outline
    assert len(outline) <= 400


@pytest.mark.asyncio
async def test_long_saved_original_has_explicit_page_and_scoped_continuation(phone):
    activity = phone.store.ensure_agent_activity_session(
        owner_id=phone.owner_id, agent_id=phone.agent_id,
    )
    accepted = phone.store.accept_run(
        session_id=activity["session_id"], owner_id=phone.owner_id,
        agent_id=phone.agent_id, request_id="long-report", text="Make the report",
        source="scheduler", idempotency_key="long-report",
    )
    report = "\n".join(f"Item {item:03d}: " + "complete detail " * 11 for item in range(1, 151))
    phone.store.finish_request(accepted.request_id, success=True, assistant_text=report)
    message_id = phone.store.get_run(accepted.run_id, owner_id=phone.owner_id)["final_message_id"]
    phone.judgments = [{"route": "recall", "complete": True, "reply_needed": False,
                        "reply": "", "actions": [], "result_ids": [message_id]}]

    await speak(phone, "Tell me all the saved items")
    first = event_details(phone, "voice.live.action.reply_offered")[-1]["result_pages"][0]
    assert first["start"] == 0
    assert 0 < first["next_offset"] < len(report)
    staged = "\n".join(payload["content"] for _, payload in phone.updates)
    assert "Item 001" in staged and "Item 150" not in staged
    assert "not the full report" in staged

    phone.updates.clear()
    phone.judgments = [{"route": "recall", "complete": True, "reply_needed": False,
                        "reply": "", "actions": [], "result_ids": [message_id],
                        "result_continuation": True}]
    await speak(phone, "Continue that report", start=200, end=300, source="continue")
    second = event_details(phone, "voice.live.action.reply_offered")[-1]["result_pages"][0]
    assert second["start"] == first["next_offset"]
    assert second["end"] > second["start"]
    assert "Item 001" not in "\n".join(payload["content"] for _, payload in phone.updates)
    assert not phone.admit_calls


@pytest.mark.asyncio
async def test_crash_between_classification_and_admission_is_visible_unknown_not_replayed(phone):
    rows = phone.manager.actions.create(phone.binding, "not-admitted-yet", [ActionIntent("write", "Record exercise")])
    phone.manager._recover_action_relays(settle_orphans=True)
    await asyncio.sleep(0)
    persisted = action_rows(phone)[0]
    assert persisted["action_id"] == rows[0]["action_id"]
    assert persisted["status"] == "unknown" and persisted["run_id"] is None
    assert not phone.admit_calls


@pytest.mark.asyncio
async def test_readback_evidence_survives_distinct_home_workzone_and_later_switch(phone, tmp_path, monkeypatch):
    phone.judgments = [decision(action("write", "Record exercise for 30 minutes in fitness.txt"))]
    await speak(phone, "Record exercise for 30 minutes in fitness.txt")
    row = action_rows(phone)[0]
    run = phone.store.get_run(row["run_id"], owner_id=phone.owner_id)
    home, workzone, next_zone = (tmp_path / name for name in ("agent-home", "first-workzone", "next-workzone"))
    for folder in (home, workzone, next_zone):
        folder.mkdir()
    runtime = SimpleNamespace(workspace_dir=home, name=phone.agent_id, session_store=phone.store)
    registry = ToolRegistry(allowed_tools=["file_write", "apply_patch"], access_root=workzone,
        workspace_dir=workzone, secrets={}, audit_context={
            "_runtime": runtime, "owner_id": phone.owner_id, "hashi_session_id": phone.session_id,
            "hashi_run_id": row["run_id"], "request_id": run["request_id"],
        })
    result = await registry.execute("file_write", {"path": "fitness.txt", "content": "Exercise: 30 minutes\n"}, "write-exercise")
    assert not result.is_error
    assert (workzone / "fitness.txt").read_text() == "Exercise: 30 minutes\n"
    assert not (home / "tool_action_audit.jsonl").exists()
    assert event_details(phone, "voice.live.action.tool_effect")
    registry.workspace_dir = next_zone

    async def verify(_runtime, state, **_kwargs):
        supplied = state["actions"][0]
        receipt = supplied["receipts"][0]
        assert receipt["target"] == str(workzone / "fitness.txt")
        assert receipt["observed"] == (workzone / "fitness.txt").read_bytes().decode("utf-8")
        assert receipt["readback"] is True and receipt["revision"].startswith("sha256:")
        return {"actions": [{"action_id": supplied["action_id"], "verified": True,
            "evidence_refs": [receipt["evidence_ref"]], "receipt": "Recorded 30 minutes of exercise in fitness.txt."}]}

    monkeypatch.setattr(worker_actions, "invoke_phone_judgment", verify)
    inspected = await worker_actions.inspect_phone_action_results(runtime, run["request_id"], [row])
    assert inspected["actions"][0]["status"] == "verified"
    assert inspected["actions"][0]["association"] == "semantic_check"

    registry.workspace_dir = workzone
    patched = await registry.execute("apply_patch", {"path": "fitness.txt", "patch":
        "--- fitness.txt\n+++ fitness.txt\n@@ -1 +1 @@\n-Exercise: 30 minutes\n+Exercise: 40 minutes\n"}, "correct-exercise")
    assert not patched.is_error
    assert (workzone / "fitness.txt").read_text() == "Exercise: 40 minutes\n"
    observations = event_details(phone, "voice.live.action.tool_effect")
    assert observations[-1]["effect_receipt"]["observed"] == (workzone / "fitness.txt").read_bytes().decode("utf-8")
    assert observations[-1]["effect_receipt"]["revision"] != observations[0]["effect_receipt"]["revision"]


@pytest.mark.asyncio
async def test_observation_failure_does_not_fail_or_repeat_committed_write(tmp_path, monkeypatch):
    from tools import effect_receipts

    def broken(**_kwargs):
        raise OSError("diagnostic storage unavailable")

    monkeypatch.setattr(effect_receipts, "observe_tool_effect", broken)
    registry = ToolRegistry(allowed_tools=["file_write"], access_root=tmp_path, workspace_dir=tmp_path, secrets={})
    result = await registry.execute("file_write", {"path": "fitness.txt", "content": "Exercise: 30 minutes"}, "one-write")
    assert not result.is_error
    assert (tmp_path / "fitness.txt").read_text() == "Exercise: 30 minutes"
    assert not (result.details or {}).get("effect_receipt")


@pytest.mark.asyncio
async def test_unrelated_success_and_forged_or_reused_receipts_do_not_certify_actions(tmp_path, monkeypatch):
    runtime = SimpleNamespace(workspace_dir=tmp_path, name="test")
    registry = ToolRegistry(allowed_tools=["file_write"], access_root=tmp_path, workspace_dir=tmp_path,
        secrets={}, audit_context={"request_id": "req-one"})
    await registry.execute("file_write", {"path": "config.txt", "content": "theme=dark"}, "unrelated")
    candidates_seen = []

    async def refuse_unrelated(_runtime, state, **_kwargs):
        candidates_seen.extend(state["actions"])
        return {"actions": [{"action_id": "exercise", "verified": False, "evidence_refs": [], "receipt": "No exercise saved."}]}

    monkeypatch.setattr(worker_actions, "invoke_phone_judgment", refuse_unrelated)
    result = await worker_actions.inspect_phone_action_results(runtime, "req-one",
        [{"action_id": "exercise", "kind": "write", "request": "Record exercise for 30 minutes"}])
    assert candidates_seen[0]["receipts"][0]["observed"] == "theme=dark"
    assert result["actions"][0]["status"] == "unknown"
    assert not effect_evidence("write", {"tool_actions": [
        {"status": "success", "effect_receipt": {"kind": "write", "evidence_ref": "fake"}}]})

    async def forge(_runtime, _state, **_kwargs):
        return {"actions": [{"action_id": "exercise", "verified": True, "evidence_refs": ["invented"], "receipt": "Saved."}]}
    monkeypatch.setattr(worker_actions, "invoke_phone_judgment", forge)
    assert (await worker_actions.inspect_phone_action_results(runtime, "req-one",
        [{"action_id": "exercise", "kind": "write", "request": "Record exercise"}]))["actions"][0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_reply_ack_is_not_audible_and_lost_ack_has_bounded_resume(phone):
    calls = []

    async def lost_ack(_binding, **payload):
        assert payload["kind"] == "commentary" and payload["delegation_id"] is None
        calls.append(payload["content"])
        return False

    phone.manager._send_provider_update = lost_ack
    for _ in range(4):
        await phone.manager._offer_action_reply(phone.binding, "specific-result", "The existing record contains 40 minutes.")
    assert calls == ["The existing record contains 40 minutes."] * 2
    assert len(event_details(phone, "voice.live.action.reply_attempted")) == 2
    assert not event_details(phone, "voice.live.action.reply_offered")
    assert event_details(phone, "voice.live.action.reply_unconfirmed")[0]["audible_delivery"] == "unverified"


@pytest.mark.asyncio
async def test_no_output_gets_one_substantive_continuation_and_then_stops(phone):
    await phone.manager._offer_action_reply(phone.binding, "answer-one", "The report has two completed checks and no incidents.")
    async def wait_until_unconfirmed():
        while not event_details(phone, "voice.live.action.reply_unconfirmed"):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait_until_unconfirmed(), timeout=2.0)
    contents = [payload["content"] for _, payload in phone.updates]
    assert contents == ["The report has two completed checks and no incidents."] * 2
    assert event_details(phone, "voice.live.action.reply_unconfirmed")
    assert not event_details(phone, "voice.live.action.reply_observed")


@pytest.mark.asyncio
async def test_transcript_observation_does_not_claim_complete_audible_playback(phone):
    await phone.manager._offer_action_reply(phone.binding, "answer-two", "Saved 40 minutes of exercise.")
    await phone.manager.append_fragment_once(phone.binding, Fragment("output", "assistant", "Saved 40 minutes of exercise.", 200, 220))
    await asyncio.sleep(0.08)
    assert len(phone.updates) == 1
    observed = event_details(phone, "voice.live.action.reply_observed")[0]
    assert observed["generated_output"] == "observed"
    assert observed["content_delivery"] == observed["audible_delivery"] == "unverified"


def test_external_record_modification_and_cancellation_can_use_resolved_targets():
    for kind in ("modify", "cancel"):
        parsed = parse_decision(decision(action(kind, "The saved reminder with title Gym at 7 pm")),
                                known_action_ids=set())
        assert parsed.actions[0].kind == kind
        assert parsed.actions[0].target_action_id is None

@pytest.mark.asyncio
async def test_invalid_judgment_for_superseded_speech_cannot_consume_file_target(phone):
    prefix = "This is a test. Using synthetic exercise data only, please create a new text file named phone test September thirty in your permitted working folder. Record exactly"
    suffix = ": Exercise forty minutes. Read the saved file back. Then tell me what it contains."
    started, release = asyncio.Event(), asyncio.Event()
    heard = []

    async def judge(_binding, state):
        heard.append(state["utterance"])
        if len(heard) == 1:
            started.set()
            await release.wait()
            return {"route": "act", "complete": False, "reply": "", "actions": []}
        return decision(action("write", "Create phone test September thirty containing exactly Exercise forty minutes, then read the file back"))

    phone.manager._judge_action = judge
    first = asyncio.create_task(speak(phone, prefix, end=21400, source="prefix"))
    await started.wait()
    await phone.manager.append_fragment_once(phone.binding, Fragment("suffix", "user", suffix, 22200, 27600))
    release.set()
    await first
    routed = event_details(phone, "voice.live.delegation.routed")
    assert routed[-1]["decision"] == "superseded"
    discarded = event_details(phone, "voice.live.action.judgment_discarded")[0]
    assert discarded["reason"] == "new_speech"
    assert discarded["shape"]["complete"] is False
    assert not phone.updates
    await phone.manager.register_delegation_once(phone.binding, "complete-request", 27601)
    await phone.manager.schedule_proposal(phone.binding, "complete-request", 27601)
    assert heard == [prefix, prefix + suffix]
    assert len(phone.admit_calls) == 1
    assert "phone test September thirty" in phone.admitted_proposals[0].execution_text
    assert "Exercise forty minutes" in phone.admitted_proposals[0].execution_text


@pytest.mark.asyncio
async def test_invalid_unexecuted_judgment_retains_prefix_for_later_speech(phone):
    prefix = "Create a new file named phone test September thirty. Record exactly"
    suffix = ": Exercise forty minutes. Read the saved file back."
    heard = []

    async def judge(_binding, state):
        heard.append(state["utterance"])
        if len(heard) == 1:
            return {"route": "act", "complete": False, "reply": "", "actions": []}
        return decision(action("write", "Create phone test September thirty with Exercise forty minutes and read it back"))

    phone.manager._judge_action = judge
    await speak(phone, prefix, end=21400, source="prefix")
    assert not phone.admit_calls
    assert event_details(phone, "voice.live.delegation.routed")[-1]["decision"] == "judgment_failed"
    assert event_details(phone, "voice.live.action.judgment_rejected")[0]["shape"]["route"] == "act"
    await phone.manager.register_delegation_once(phone.binding, "duplicate-prefix", 21401)
    await phone.manager.schedule_proposal(phone.binding, "duplicate-prefix", 21401)
    assert heard == [prefix]  # No unbounded retry of the same failed judgment.
    assert event_details(phone, "voice.live.delegation.routed")[-1]["decision"] == "judgment_failed"
    await speak(phone, suffix, start=22200, end=27600, source="suffix")
    assert heard == [prefix, prefix + suffix]
    assert len(phone.admit_calls) == 1


@pytest.mark.asyncio
async def test_tool_effect_before_admission_ack_uses_validated_run_origin(phone, tmp_path):
    runtime = SimpleNamespace(workspace_dir=tmp_path / "home", name=phone.agent_id, session_store=phone.store)
    runtime.workspace_dir.mkdir()
    workzone = tmp_path / "workzone"
    workzone.mkdir()

    async def admit_before_ack(binding, proposal, key):
        origin = phone.store.resolve_live_voice_origin(owner_id=binding.owner_id,
            session_id=binding.session_id, agent_id=binding.agent_id,
            context_generation=binding.context_generation, candidate={
                "call_id": binding.call_id, "call_epoch": binding.call_epoch,
                "delegation_id": proposal.delegation_id, "proposal_version": proposal.version,
                "proposal_digest": proposal.digest})
        accepted = phone.store.accept_run(session_id=binding.session_id, owner_id=binding.owner_id,
            agent_id=binding.agent_id, request_id="fast-write-before-ack", text=proposal.text,
            source="session-api", idempotency_key=key, expected_context_generation=binding.context_generation,
            message_context={"live_voice": origin})
        assert action_rows(phone)[0]["run_id"] is None
        registry = ToolRegistry(allowed_tools=["file_write"], access_root=workzone, workspace_dir=workzone,
            secrets={}, audit_context={"_runtime": runtime, "owner_id": phone.owner_id,
                "hashi_session_id": phone.session_id, "hashi_run_id": accepted.run_id,
                "request_id": accepted.request_id})
        result = await registry.execute("file_write",
            {"path": "fitness.txt", "content": "Exercise: 40 minutes\n"}, "fast-write")
        assert not result.is_error
        assert event_details(phone, "voice.live.action.tool_effect")[0]["effect_receipt"]["readback"]
        return {"request_id": accepted.request_id, "run_id": accepted.run_id, "message_id": accepted.message_id}

    phone.manager._admit_run = admit_before_ack
    phone.judgments = [decision(action("write", "Record 40 minutes of exercise in fitness.txt"))]
    await speak(phone, "Record 40 minutes of exercise in fitness.txt")
    assert (workzone / "fitness.txt").read_text() == "Exercise: 40 minutes\n"
    assert action_rows(phone)[0]["run_id"] is not None
    assert len(event_details(phone, "voice.live.action.tool_effect")) == 1


@pytest.mark.asyncio
async def test_effect_reply_uses_frozen_phone_locale_and_stale_judgment_is_rejected(phone, tmp_path, monkeypatch):
    phone.judgments = [decision(action("query", "Check the saved fitness entry"))]
    await speak(phone, "Check the saved fitness entry")
    row = action_rows(phone)[0]
    run = phone.store.get_run(row["run_id"], owner_id=phone.owner_id)
    runtime = SimpleNamespace(workspace_dir=tmp_path, name=phone.agent_id, session_store=phone.store)
    with phone.store._lock, phone.store._connection() as connection:
        connection.execute("UPDATE live_calls SET phone_config_json=?,call_epoch=2 WHERE call_id=?",
            (json.dumps({"public":{"language":"auto","interface_language":"zh-CN"}}), phone.call_id))

    async def inspect(_runtime, request_id, actions, **options):
        assert request_id == run["request_id"]
        assert actions[0]["action_id"] == row["action_id"]
        assert options["reply_language"] == "auto"
        assert options["fallback_language"] == "zh-CN"
        return {"actions":[]}

    monkeypatch.setattr(worker_actions, "inspect_phone_action_results", inspect)
    await worker_actions.handle_phone_action_operation(runtime, "inspect", {
        "owner_id":phone.owner_id, "scope":phone.scope,
        "request_id":run["request_id"], "actions":[row]})
    with pytest.raises(Exception, match="live_scope_changed"):
        await worker_actions.handle_phone_action_operation(runtime, "judge",
            {"owner_id":phone.owner_id, "scope":phone.scope, "state":{"utterance":"Record that"}})
