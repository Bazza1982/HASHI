from __future__ import annotations

import asyncio
import threading
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from orchestrator import (
    frontend_ingress,
    runtime_delivery_order,
    runtime_lifecycle,
    runtime_session,
)
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.request_activity import RequestActivityStore
from orchestrator.session_store import SessionStore


def _admission_runtime(tmp_path):
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "audit-agent"
    runtime.global_config = SimpleNamespace(
        project_root=tmp_path,
        bridge_home=tmp_path,
        instance_id="HASHI3",
        authorized_id=42,
    )
    runtime.config = SimpleNamespace()
    runtime.backend_manager = SimpleNamespace(_read_state_dict=lambda: {})
    runtime._execution_admissions = {}
    runtime.next_request_id = lambda: "req-audit-handoff"
    runtime.session_store = SessionStore(
        tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI3"
    )
    runtime.message_logger = Mock()
    runtime.logger = Mock()
    runtime.error_logger = Mock()
    runtime.request_activity = RequestActivityStore()
    runtime.queue = asyncio.Queue()
    return runtime


def _terminal_kinds(store, session_id, owner):
    return [
        event["kind"]
        for event in store.events(session_id, owner_id=owner)
        if str(event["kind"]).startswith("run.")
        and event["kind"] not in {"run.accepted", "run.started"}
    ]


@pytest.mark.asyncio
async def test_real_enqueue_binds_workspace_metadata_and_queue_to_receipt_generation(
    tmp_path, monkeypatch
):
    runtime = _admission_runtime(tmp_path)
    original_ingress = frontend_ingress.accept_runtime_ingress
    original_workspace = runtime.session_store.session_workspace
    workspace_calls = []

    def return_stale_resolved_session(*args, **kwargs):
        session, accepted, owner, surface, channel = original_ingress(
            *args, **kwargs
        )
        stale = dict(session)
        stale["context_generation"] = accepted.context_generation + 40
        return stale, accepted, owner, surface, channel

    def capture_workspace(session_id, context_generation):
        workspace_calls.append((session_id, context_generation))
        return original_workspace(session_id, context_generation)

    monkeypatch.setattr(
        frontend_ingress,
        "accept_runtime_ingress",
        return_stale_resolved_session,
    )
    monkeypatch.setattr(runtime.session_store, "session_workspace", capture_workspace)
    monkeypatch.setattr(
        runtime_session, "session_workzone_state", lambda *_args, **_kwargs: {}
    )

    request_id = await runtime.enqueue_request(
        42,
        "bind every consumer to the canonical receipt",
        "api",
        "receipt generation",
        idempotency_key="receipt-generation",
        deliver_to_telegram=False,
    )
    item = runtime.queue.get_nowait()
    run = runtime.session_store.find_run_by_idempotency(
        session_id=item.session_id,
        owner_id="user:42",
        idempotency_key="receipt-generation",
    )

    assert request_id == run["request_id"] == item.request_id
    assert item.context_generation == run["context_generation"] == 1
    assert item.request_metadata["context_generation"] == 1
    assert item.request_metadata["hashi_session_id"] == run["session_id"]
    assert workspace_calls == [(run["session_id"], 1)]


@pytest.mark.asyncio
async def test_concurrent_real_enqueues_share_one_canonical_receipt_and_queue_item(
    tmp_path, monkeypatch
):
    runtime = _admission_runtime(tmp_path)
    sequence_lock = threading.Lock()
    sequence = 0

    def next_request_id():
        nonlocal sequence
        with sequence_lock:
            sequence += 1
            return f"req-concurrent-{sequence}"

    runtime.next_request_id = next_request_id
    monkeypatch.setattr(
        runtime_session, "session_workzone_state", lambda *_args, **_kwargs: {}
    )
    original_find = runtime.session_store.find_run_by_idempotency
    lookup_barrier = threading.Barrier(2)

    def concurrent_miss(**kwargs):
        result = original_find(**kwargs)
        if kwargs.get("idempotency_key") == "concurrent-client-intent" and result is None:
            lookup_barrier.wait(timeout=3)
        return result

    monkeypatch.setattr(
        runtime.session_store, "find_run_by_idempotency", concurrent_miss
    )

    async def enqueue():
        return await runtime.enqueue_request(
            42,
            "same concurrent request",
            "api",
            "concurrent admission",
            idempotency_key="concurrent-client-intent",
            deliver_to_telegram=False,
        )

    results = await asyncio.gather(enqueue(), enqueue())
    session = runtime.session_store.ensure_default_session(
        owner_id="user:42", agent_id=runtime.name
    )
    runs = runtime.session_store.recent_session_runs(
        session["session_id"], owner_id="user:42"
    )

    assert results[0] == results[1] == runs[0]["request_id"]
    assert len(runs) == 1
    assert len(runtime.session_store.messages(session["session_id"])) == 1
    assert runtime.queue.qsize() == 1
    assert runtime.queue.get_nowait().request_id == runs[0]["request_id"]


@pytest.mark.asyncio
async def test_real_enqueue_preparation_failure_closes_original_run_once(
    tmp_path, monkeypatch
):
    runtime = _admission_runtime(tmp_path)
    monkeypatch.setattr(
        runtime_session, "session_workzone_state", lambda *_args, **_kwargs: {}
    )
    original_workspace = runtime.session_store.session_workspace
    calls = 0

    def fail_workspace_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected workspace failure after durable admission")
        return original_workspace(*args, **kwargs)

    monkeypatch.setattr(runtime.session_store, "session_workspace", fail_workspace_once)

    with pytest.raises(OSError, match="workspace failure"):
        await runtime.enqueue_request(
            42,
            "perform durable work",
            "api",
            "audit handoff",
            idempotency_key="audit-handoff-key",
            deliver_to_telegram=False,
        )

    reopened = SessionStore(runtime.session_store.db_path, instance_id="HASHI3")
    session = reopened.ensure_default_session(owner_id="user:42", agent_id=runtime.name)
    run = reopened.find_run_by_idempotency(
        session_id=session["session_id"],
        owner_id="user:42",
        idempotency_key="audit-handoff-key",
    )
    assert run["state"] == "failed"
    assert runtime.queue.empty()
    assert _terminal_kinds(reopened, session["session_id"], "user:42") == [
        "run.failed"
    ]

    replayed_request_id = await runtime.enqueue_request(
        42,
        "perform durable work",
        "api",
        "audit handoff",
        idempotency_key="audit-handoff-key",
        deliver_to_telegram=False,
    )

    assert replayed_request_id == run["request_id"]
    assert runtime.queue.empty()
    assert _terminal_kinds(reopened, session["session_id"], "user:42") == [
        "run.failed"
    ]


@pytest.mark.asyncio
async def test_cancel_during_real_admission_joins_original_commit_and_closes_it(
    tmp_path, monkeypatch
):
    runtime = _admission_runtime(tmp_path)
    committed = threading.Event()
    release = threading.Event()
    original_accept = runtime.session_store.accept_run

    def accept_then_pause(*args, **kwargs):
        receipt = original_accept(*args, **kwargs)
        committed.set()
        assert release.wait(timeout=3)
        return receipt

    monkeypatch.setattr(runtime.session_store, "accept_run", accept_then_pause)
    monkeypatch.setattr(
        runtime_session, "session_workzone_state", lambda *_args, **_kwargs: {}
    )
    task = asyncio.create_task(
        runtime.enqueue_request(
            42,
            "cancel during admission",
            "api",
            "cancel admission",
            idempotency_key="cancel-during-admission",
            deliver_to_telegram=False,
        )
    )
    assert await asyncio.to_thread(committed.wait, 3)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    session = runtime.session_store.ensure_default_session(
        owner_id="user:42", agent_id=runtime.name
    )
    run = runtime.session_store.find_run_by_idempotency(
        session_id=session["session_id"],
        owner_id="user:42",
        idempotency_key="cancel-during-admission",
    )
    assert run["state"] == "interrupted"
    assert runtime.queue.empty()


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_orphan_admission_or_exact_cleanup(
    tmp_path, monkeypatch
):
    runtime = _admission_runtime(tmp_path)
    committed = threading.Event()
    release_admission = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    cleanup_calls = 0
    original_accept = runtime.session_store.accept_run
    original_cleanup = runtime.session_store.fail_unsubmitted_request

    def accept_then_pause(*args, **kwargs):
        receipt = original_accept(*args, **kwargs)
        committed.set()
        assert release_admission.wait(timeout=3)
        return receipt

    def cleanup_then_pause(*args, **kwargs):
        nonlocal cleanup_calls
        cleanup_calls += 1
        cleanup_started.set()
        assert release_cleanup.wait(timeout=3)
        return original_cleanup(*args, **kwargs)

    monkeypatch.setattr(runtime.session_store, "accept_run", accept_then_pause)
    monkeypatch.setattr(
        runtime.session_store, "fail_unsubmitted_request", cleanup_then_pause
    )
    monkeypatch.setattr(
        runtime_session, "session_workzone_state", lambda *_args, **_kwargs: {}
    )
    task = asyncio.create_task(
        runtime.enqueue_request(
            42,
            "cancel repeatedly during admission",
            "api",
            "repeat cancel admission",
            idempotency_key="repeat-cancel-admission",
            deliver_to_telegram=False,
        )
    )
    assert await asyncio.to_thread(committed.wait, 3)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    release_admission.set()
    assert await asyncio.to_thread(cleanup_started.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    session = runtime.session_store.ensure_default_session(
        owner_id="user:42", agent_id=runtime.name
    )
    run = runtime.session_store.find_run_by_idempotency(
        session_id=session["session_id"],
        owner_id="user:42",
        idempotency_key="repeat-cancel-admission",
    )
    assert cleanup_calls == 1
    assert run["state"] == "interrupted"
    assert runtime.queue.empty()
    assert _terminal_kinds(runtime.session_store, session["session_id"], "user:42") == [
        "run.interrupted"
    ]


def test_unsubmitted_cleanup_never_overwrites_executor_or_terminal_winner(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    owner = "user:42"
    session = store.create_session(owner_id=owner, agent_id="audit-agent")

    def accept(request_id):
        return store.accept_run(
            session_id=session["session_id"],
            owner_id=owner,
            agent_id="audit-agent",
            request_id=request_id,
            text="work",
            source="session-api",
            idempotency_key=request_id,
        )

    running = accept("req-running-winner")
    token = store.mark_request_running(running.request_id, worker_id="winner")
    unchanged = store.fail_unsubmitted_request(
        running.request_id,
        session_id=running.session_id,
        run_id=running.run_id,
        message_id=running.message_id,
        context_generation=running.context_generation,
        owner_id=owner,
        agent_id="audit-agent",
        error_text="producer failed",
    )
    assert token == 1
    assert unchanged["state"] == "running"

    stopped = accept("req-terminal-winner")
    stop_token = store.mark_request_running(stopped.request_id, worker_id="winner")
    store.finish_request(
        stopped.request_id,
        success=False,
        error_text="stopped first",
        failure_state="stopped",
        fencing_token=stop_token,
    )
    unchanged = store.fail_unsubmitted_request(
        stopped.request_id,
        session_id=stopped.session_id,
        run_id=stopped.run_id,
        message_id=stopped.message_id,
        context_generation=stopped.context_generation,
        owner_id=owner,
        agent_id="audit-agent",
        error_text="producer failed late",
    )
    assert unchanged["state"] == "stopped"


def _listener_runtime(store, *, owner, agent, request_id, fencing_token):
    runtime = SimpleNamespace(
        name=agent,
        global_config=SimpleNamespace(authorized_id=42),
        config=SimpleNamespace(active_backend="her-v2"),
        backend_manager=SimpleNamespace(current_backend=None),
        session_store=store,
        session_dir=store.db_path.parent,
        _request_meta_by_id={
            request_id: {
                "owner_id": owner,
                "agent_id": agent,
                "hashi_fencing_token": fencing_token,
            }
        },
        _request_listeners={},
        _pending_request_results={},
        request_activity=RequestActivityStore(),
        logger=Mock(),
    )
    runtime.request_activity.start(request_id)
    runtime._notify_request_listeners = MethodType(
        FlexibleAgentRuntime._notify_request_listeners, runtime
    )
    return runtime


@pytest.mark.asyncio
async def test_post_commit_binding_and_listener_failures_keep_canonical_success(
    tmp_path, monkeypatch
):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI3")
    owner = "user:42"
    agent = "audit-agent"
    session = store.create_session(owner_id=owner, agent_id=agent)
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id=agent,
        request_id="req-canonical-success",
        text="work",
        source="session-api",
        idempotency_key="canonical-success",
    )
    token = store.mark_request_running(accepted.request_id, worker_id="worker")
    runtime = _listener_runtime(
        store,
        owner=owner,
        agent=agent,
        request_id=accepted.request_id,
        fencing_token=token,
    )
    observed = []

    def broken_listener(_payload):
        raise OSError("listener unavailable")

    runtime._request_listeners[accepted.request_id] = [
        broken_listener,
        lambda payload: observed.append(dict(payload)),
    ]
    monkeypatch.setattr(
        runtime_session,
        "capture_backend_binding",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("binding unavailable")),
    )
    monkeypatch.setattr(
        "orchestrator.runtime_media.finish_native_voice_transcript_path", AsyncMock()
    )
    monkeypatch.setattr(
        "orchestrator.frontend_whatsapp_mirror.deliver_whatsapp_mirror", AsyncMock()
    )
    monkeypatch.setattr(
        "orchestrator.runtime_debug_reporting.schedule_terminal_diagnostic",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "orchestrator.runtime_debug_reporting.schedule_failure_report",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "orchestrator.terminal_console.finish_request", lambda *_args, **_kwargs: None
    )

    await runtime._notify_request_listeners(
        accepted.request_id,
        {"success": True, "text": "canonical answer", "source": "session-api"},
    )

    assert len(observed) == 1
    assert observed[0]["success"] is True
    assert observed[0]["text"] == "canonical answer"
    assert observed[0]["content"] == [
        {"type": "text", "text": "canonical answer"}
    ]
    assert observed[0]["continuity_status"] == "recovery_required"
    assert observed[0]["terminal_side_errors"] == [
        {"phase": "backend_binding", "error_type": "OSError"}
    ]
    assert store.get_run(accepted.run_id)["state"] == "completed"
    assert _terminal_kinds(store, session["session_id"], owner) == ["run.completed"]


@pytest.mark.parametrize("winner", ["completed", "stopped"])
def test_malformed_late_proposal_projects_canonical_terminal_winner(
    tmp_path, monkeypatch, winner
):
    store = SessionStore(tmp_path / f"{winner}.sqlite3", instance_id="HASHI3")
    owner = "user:42"
    agent = "audit-agent"
    session = store.create_session(owner_id=owner, agent_id=agent)
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id=agent,
        request_id=f"req-{winner}",
        text="work",
        source="session-api",
        idempotency_key=f"winner-{winner}",
    )
    token = store.mark_request_running(accepted.request_id, worker_id="worker")
    if winner == "completed":
        store.finish_request(
            accepted.request_id,
            success=True,
            assistant_text="winner answer",
            fencing_token=token,
        )
    else:
        store.finish_request(
            accepted.request_id,
            success=False,
            error_text="stopped first",
            failure_state="stopped",
            fencing_token=token,
        )
    runtime = _listener_runtime(
        store,
        owner=owner,
        agent=agent,
        request_id=accepted.request_id,
        fencing_token=token,
    )
    monkeypatch.setattr(runtime_session, "capture_backend_binding", lambda *_a, **_k: None)

    resolution = runtime_session.finish_request_from_listener(
        runtime,
        accepted.request_id,
        {
            "success": True,
            "text": "late answer",
            "content": [{"type": "text", "text": 17}],
        },
    )

    assert resolution.side_errors == (
        {"phase": "terminal_commit", "error_type": "ValueError"},
    )
    assert resolution.payload["terminal_state"] == winner
    if winner == "completed":
        assert resolution.payload["success"] is True
        assert resolution.payload["text"] == "winner answer"
    else:
        assert resolution.payload["success"] is False
        assert resolution.payload["interrupted"] is True
        assert resolution.payload["interrupt_reason"] == "user_stop"
        assert resolution.payload["text"] is None


def test_invalid_proposal_for_active_run_is_unresolved_and_never_terminalized(
    tmp_path
):
    store = SessionStore(tmp_path / "active.sqlite3", instance_id="HASHI3")
    owner = "user:42"
    agent = "audit-agent"
    session = store.create_session(owner_id=owner, agent_id=agent)
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id=agent,
        request_id="req-active-invalid",
        text="work",
        source="session-api",
        idempotency_key="active-invalid",
    )
    token = store.mark_request_running(accepted.request_id, worker_id="worker")
    runtime = _listener_runtime(
        store,
        owner=owner,
        agent=agent,
        request_id=accepted.request_id,
        fencing_token=token,
    )

    with pytest.raises(runtime_session.TerminalResolutionPending):
        runtime_session.finish_request_from_listener(
            runtime,
            accepted.request_id,
            {
                "success": True,
                "content": [{"type": "text", "text": 17}],
            },
        )
    assert store.get_run(accepted.run_id)["state"] == "running"


@pytest.mark.asyncio
async def test_remote_policy_rejection_finishes_canonical_run_without_provider(
    tmp_path, monkeypatch
):
    from tests.test_runtime_pipeline import _item, _runtime

    runtime = _runtime()
    runtime.session_store = SessionStore(
        tmp_path / "policy.sqlite3", instance_id="HASHI1"
    )
    owner = "owner"
    session = runtime.session_store.create_session(
        owner_id=owner, agent_id=runtime.name, title="policy"
    )
    accepted = runtime.session_store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id=runtime.name,
        request_id="req-policy-block",
        text="blocked work",
        source="session-api",
        idempotency_key="policy-block",
    )
    item = _item(
        request_id=accepted.request_id,
        session_id=session["session_id"],
        run_id=accepted.run_id,
        context_generation=1,
        owner_id=owner,
        source="session-api",
        session_surface="workbench",
        session_channel_key="default",
        deliver_to_telegram=False,
    )
    runtime.queue = asyncio.Queue()
    runtime.request_activity = RequestActivityStore()
    runtime.request_activity.start(item.request_id)
    runtime._request_listeners = {}
    runtime._pending_request_results = {}
    runtime._notify_request_listeners = MethodType(
        FlexibleAgentRuntime._notify_request_listeners, runtime
    )
    runtime._remote_backend_block_reason = lambda _source: "policy rejected source"
    monkeypatch.setattr(
        "orchestrator.runtime_media.finish_native_voice_transcript_path", AsyncMock()
    )
    monkeypatch.setattr(
        "orchestrator.frontend_whatsapp_mirror.deliver_whatsapp_mirror", AsyncMock()
    )
    monkeypatch.setattr(
        "orchestrator.runtime_debug_reporting.schedule_terminal_diagnostic",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "orchestrator.runtime_debug_reporting.schedule_failure_report",
        lambda *_a, **_k: None,
    )

    await runtime.queue.put(item)
    task = asyncio.create_task(runtime_lifecycle.process_queue(runtime))
    try:
        await asyncio.wait_for(runtime.queue.join(), timeout=2)
    finally:
        task.cancel()
        await task

    run = runtime.session_store.get_run(accepted.run_id)
    detail = runtime.session_store.request_failure_detail(
        accepted.request_id, owner_id=owner, agent_id=runtime.name
    )
    assert run["state"] == "failed"
    assert detail["error_code"] == "REMOTE_BACKEND_BLOCKED"
    assert runtime.backend_manager.calls == []


@pytest.mark.asyncio
async def test_lost_execution_claim_never_starts_provider(tmp_path):
    from tests.test_runtime_pipeline import _item, _runtime

    runtime = _runtime()
    runtime.session_store = SessionStore(
        tmp_path / "claim.sqlite3", instance_id="HASHI1"
    )
    owner = "owner"
    session = runtime.session_store.create_session(
        owner_id=owner, agent_id=runtime.name, title="claim"
    )
    accepted = runtime.session_store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id=runtime.name,
        request_id="req-claim-lost",
        text="one owner only",
        source="session-api",
        idempotency_key="claim-lost",
    )
    runtime.session_store.mark_request_running(
        accepted.request_id, worker_id="other-worker"
    )
    item = _item(
        request_id=accepted.request_id,
        session_id=session["session_id"],
        run_id=accepted.run_id,
        context_generation=1,
        owner_id=owner,
        source="session-api",
        session_surface="workbench",
        session_channel_key="default",
    )
    runtime.queue = asyncio.Queue()
    await runtime.queue.put(item)

    task = asyncio.create_task(runtime_lifecycle.process_queue(runtime))
    try:
        await asyncio.wait_for(runtime.queue.join(), timeout=2)
    finally:
        task.cancel()
        await task

    assert runtime.session_store.get_run(accepted.run_id)["state"] == "running"
    assert runtime.backend_manager.calls == []
    assert runtime_delivery_order.pending_sequences(runtime, item.chat_id) == []
