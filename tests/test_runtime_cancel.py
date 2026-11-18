from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import runtime_cancel, runtime_control, runtime_pipeline
from orchestrator.frontend_command_context import bind_frontend_command_principal
from orchestrator.session_store import SessionStore
from orchestrator.telegram_actor_scope import TelegramActorScope, _current_actor


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["telegram", "enterprise", "local"])
async def test_scoped_stop_cancels_only_authenticated_owners_runs(tmp_path, monkeypatch, transport):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI1")
    owner = {"telegram": "telegram-user:21:abc", "enterprise": "enterprise:alice",
             "local": "local:alice"}[transport]
    mine = store.ensure_default_session(owner_id=owner, agent_id="sakura")
    mine_other = store.create_session(owner_id=owner, agent_id="sakura")
    other = store.ensure_default_session(owner_id="user:primary", agent_id="sakura")

    def accept(session, session_owner, suffix):
        return store.accept_run(session_id=session["session_id"], owner_id=session_owner,
                                agent_id="sakura", request_id=f"req-{suffix}", text="work",
                                source="workbench", idempotency_key=suffix)

    first = accept(mine, owner, "one")
    second = accept(mine_other, owner, "two")
    foreign = accept(other, "user:primary", "other")
    runtime = SimpleNamespace(
        name="sakura", session_store=store, queue=asyncio.Queue(),
        config=SimpleNamespace(active_backend="codex-cli"),
        global_config=SimpleNamespace(owner_scope_id="user:primary"),
        logger=logging.getLogger("test"), _is_authorized_user=lambda _: True,
        _reply_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=21 if transport == "telegram" else owner),
        effective_chat=SimpleNamespace(id=21), _hashi_session_owner_id=owner,
        _hashi_session_id=mine["session_id"],
        _hashi_session_surface="telegram" if transport == "telegram" else "workbench",
    )
    def no_global_stop(*_args, **_kwargs):
        raise AssertionError("scoped user must not advance the agent-wide stop fence")
    monkeypatch.setattr(runtime_control, "advance_runtime_fence", no_global_stop)
    if transport == "telegram":
        token = _current_actor.set(TelegramActorScope(runtime, 21, owner, False, False))
        try:
            await runtime_control.cmd_stop(runtime, update, SimpleNamespace(args=[]))
        finally:
            _current_actor.reset(token)
    else:
        with bind_frontend_command_principal(runtime, owner):
            await runtime_control.cmd_stop(runtime, update, SimpleNamespace(args=[]))

    assert store.get_run(first.run_id, owner_id=owner)["state"] == "stopped"
    assert store.get_run(second.run_id, owner_id=owner)["state"] == "stopped"
    assert store.get_run(foreign.run_id, owner_id="user:primary")["state"] == "queued"


@pytest.mark.asyncio
async def test_scoped_stop_rejects_foreign_selected_session(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI1")
    foreign = store.ensure_default_session(owner_id="user:primary", agent_id="sakura")
    run = store.accept_run(session_id=foreign["session_id"], owner_id="user:primary",
                           agent_id="sakura", request_id="foreign", text="work",
                           source="workbench", idempotency_key="foreign")
    runtime = SimpleNamespace(
        name="sakura", session_store=store, queue=asyncio.Queue(),
        config=SimpleNamespace(active_backend="codex-cli"),
        global_config=SimpleNamespace(owner_scope_id="user:primary"),
        logger=logging.getLogger("test"), _is_authorized_user=lambda _: True,
        _reply_text=AsyncMock(),
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id="enterprise:alice"),
                             effective_chat=SimpleNamespace(id="default"),
                             _hashi_session_owner_id="user:primary",
                             _hashi_session_id=foreign["session_id"],
                             _hashi_session_surface="workbench")
    monkeypatch.setattr(runtime_control, "advance_runtime_fence",
                        lambda *_a, **_k: pytest.fail("advanced global stop fence"))
    with bind_frontend_command_principal(runtime, "enterprise:alice"):
        result = await runtime_control.cmd_stop(runtime, update, SimpleNamespace(args=[]))
    assert result["ok"] is False
    assert store.get_run(run.run_id, owner_id="user:primary")["state"] == "queued"


@pytest.mark.asyncio
async def test_scoped_question_stop_targets_original_run_and_reports_pending(tmp_path):
    from orchestrator.run_questions import RunQuestions
    from tests.test_run_questions import active, spec

    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI1")
    session, run = active(store, req="target", agent="sakura", owner="enterprise:alice")
    _, other = active(store, req="other", agent="sakura", owner="enterprise:bob")
    questions = RunQuestions(store)
    question = questions.create(request_id=run.request_id, agent_id="sakura", payload=spec())
    questions.answer(session_id=session["session_id"], owner_id="enterprise:alice",
                     question_id=question["question_id"],
                     payload={"idempotency_key": "stop", "option_id": "__hashi_stop__"})
    target_task = asyncio.create_task(asyncio.Event().wait())
    other_task = asyncio.create_task(asyncio.Event().wait())
    runtime = SimpleNamespace(
        name="sakura", session_store=store, queue=asyncio.Queue(),
        config=SimpleNamespace(active_backend="codex-cli"),
        global_config=SimpleNamespace(owner_scope_id="user:primary"),
        logger=logging.getLogger("test"), _is_authorized_user=lambda _: True,
        _reply_text=AsyncMock(),
        current_request_meta={"request_id": run.request_id, "hashi_session_id": session["session_id"],
                              "hashi_run_id": run.run_id},
        _generation_tasks_by_request={run.request_id: target_task, other.request_id: other_task},
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id="enterprise:alice"))
    context = SimpleNamespace(_hashi_question_stop_id=question["question_id"])
    try:
        with bind_frontend_command_principal(runtime, "enterprise:bob"):
            rejected = await runtime_control.cmd_stop(runtime, update, context)
        assert rejected["ok"] is False
        assert not target_task.cancelling()
        with bind_frontend_command_principal(runtime, "enterprise:alice"):
            result = await runtime_control.cmd_stop(runtime, update, context)
        assert result == {"ok": False, "error_code": "question_stop_unconfirmed"}
        assert target_task.cancelling() > 0
        assert not other_task.cancelling()
        assert store.get_run(run.run_id)["state"] == "running"
        assert store.get_run(other.run_id)["state"] == "running"
    finally:
        target_task.cancel()
        other_task.cancel()
        await asyncio.gather(target_task, other_task, return_exceptions=True)


class _Store:
    def __init__(self, *, request_id="req-target", state="running"):
        self.run = {"owner_id": "owner", "session_id": "ses-one", "agent_id": "sakura",
                    "run_id": "run-one", "request_id": request_id, "state": state}

    def get_run(self, run_id, *, owner_id=None):
        assert run_id == "run-one"
        assert owner_id in (None, "owner")
        return dict(self.run)

    def cancel_run(self, run_id, *, owner_id, reason):
        assert (run_id, owner_id, reason) == ("run-one", "owner", "cancelled_by_user")
        self.run["state"] = "stopped"
        return dict(self.run)


def _runtime(store):
    runtime = SimpleNamespace(name="sakura", queue=asyncio.Queue(), current_request_meta=None)

    async def notify(request_id, payload):
        assert request_id == store.run["request_id"]
        assert payload["interrupted"] is True
        store.run["state"] = "stopped"

    runtime._notify_request_listeners = notify
    return runtime


@pytest.mark.asyncio
async def test_exact_cancel_after_final_result_claim_is_honestly_rejected(monkeypatch):
    store = _Store()
    runtime = _runtime(store)
    monkeypatch.setattr(runtime_cancel.runtime_session, "ensure_store", lambda _: store)
    assert await runtime_cancel.claim_final_result(runtime, SimpleNamespace(request_id="req-target"))
    result = await runtime_cancel.cancel_session_run(
        runtime, owner_id="owner", session_id="ses-one", run_id="run-one", request_id="req-target"
    )
    assert not result["ok"]
    assert result["error_code"] == "final_result_committed"
    assert "req-target" not in runtime_cancel.requested_ids(runtime)
    assert store.run["state"] == "running"


@pytest.mark.asyncio
async def test_accepted_exact_cancel_prevents_final_result_claim(monkeypatch):
    store = _Store()
    runtime = _runtime(store)
    runtime.current_request_meta = {"request_id": "req-target", "hashi_session_id": "ses-one", "hashi_run_id": "run-one"}
    monkeypatch.setattr(runtime_cancel.runtime_session, "ensure_store", lambda _: store)
    result = await runtime_cancel.cancel_session_run(
        runtime, owner_id="owner", session_id="ses-one", run_id="run-one", request_id="req-target"
    )
    assert result["status"] == "cancellation_requested"
    assert not await runtime_cancel.claim_final_result(runtime, SimpleNamespace(request_id="req-target"))
    assert await runtime_cancel.claim_final_result(runtime, SimpleNamespace(request_id="req-other"))


@pytest.mark.asyncio
async def test_cancel_run_cancels_only_selected_generation(monkeypatch):
    store = _Store()
    monkeypatch.setattr(runtime_cancel.runtime_session, "ensure_store", lambda runtime: store)
    runtime = _runtime(store)
    runtime.current_request_meta = {"request_id": "req-target", "hashi_session_id": "ses-one",
                                    "hashi_run_id": "run-one"}
    target = asyncio.create_task(asyncio.Event().wait())
    other = asyncio.create_task(asyncio.Event().wait())
    runtime._generation_tasks_by_request = {"req-target": target, "req-other": other}
    try:
        result = await runtime_cancel.cancel_session_run(
            runtime, owner_id="owner", session_id="ses-one", run_id="run-one",
            request_id="req-target",
        )
        assert result["status"] == "cancellation_requested"
        assert result["terminal"] is False
        assert target.cancelling() > 0
        assert not other.cancelling()
        assert store.run["state"] == "running"
    finally:
        other.cancel()
        await asyncio.gather(target, other, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_run_removes_one_exact_queued_request(monkeypatch):
    store = _Store(state="queued")
    monkeypatch.setattr(runtime_cancel.runtime_session, "ensure_store", lambda runtime: store)
    runtime = _runtime(store)
    runtime.queue.put_nowait(SimpleNamespace(request_id="req-other", session_id="ses-one"))
    runtime.queue.put_nowait(SimpleNamespace(request_id="req-target", session_id="ses-one",
                                              source="workbench", summary="target"))
    result = await runtime_cancel.cancel_session_run(
        runtime, owner_id="owner", session_id="ses-one", run_id="run-one",
        request_id="req-target",
    )
    assert result["status"] == "stopped"
    assert store.run["state"] == "stopped"
    assert runtime.queue.qsize() == 1
    assert runtime.queue.get_nowait().request_id == "req-other"


@pytest.mark.asyncio
async def test_cancel_admitted_run_before_queue_insertion_fences_later_item(monkeypatch):
    store = _Store(state="queued")
    monkeypatch.setattr(runtime_cancel.runtime_session, "ensure_store", lambda runtime: store)
    runtime = _runtime(store)
    runtime.session_store = store

    result = await runtime_cancel.cancel_session_run(
        runtime, owner_id="owner", session_id="ses-one", run_id="run-one",
        request_id="req-target",
    )

    assert result["status"] == "stopped"
    assert result["terminal"] is True
    assert runtime_cancel.queued_run_is_terminal(runtime, SimpleNamespace(
        request_id="req-target", run_id="run-one",
    ))
    assert not runtime_cancel.queued_run_is_terminal(runtime, SimpleNamespace(
        request_id="req-other", run_id="run-one",
    ))


@pytest.mark.asyncio
async def test_running_run_with_no_local_task_is_durably_fenced(monkeypatch):
    store = _Store(state="running")
    monkeypatch.setattr(runtime_cancel.runtime_session, "ensure_store", lambda runtime: store)
    runtime = _runtime(store)

    result = await runtime_cancel.cancel_session_run(
        runtime, owner_id="owner", session_id="ses-one", run_id="run-one",
        request_id="req-target",
    )

    assert result["terminal"] is True
    assert result["execution_missing"] is True
    assert store.run["state"] == "stopped"


@pytest.mark.asyncio
async def test_targeted_generation_cancel_returns_an_interrupted_response(monkeypatch):
    started = asyncio.Event()

    async def generate_response(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    runtime = SimpleNamespace(
        config=SimpleNamespace(extra={}, active_backend="codex-cli"),
        backend_manager=SimpleNamespace(current_backend=object(), generate_response=generate_response),
        is_generating=True,
    )
    monkeypatch.setattr(runtime_pipeline, "_begin_provider_session_isolation",
                        lambda _runtime, _item: SimpleNamespace(active=False))
    monkeypatch.setattr(runtime_pipeline, "_restore_provider_session_isolation",
                        lambda *_args: None)
    item = SimpleNamespace(request_id="req-target", silent=False, deliver_to_telegram=True,
                           is_retry=False, request_content=None)
    generation = asyncio.create_task(runtime_pipeline.run_backend_generation(
        runtime, item, "work", on_stream_event=None, audit_active=False,
    ))
    await started.wait()
    runtime_cancel.requested_ids(runtime).add("req-target")
    runtime._generation_tasks_by_request["req-target"].cancel()
    result = await generation
    assert result.response.is_success is False
    assert result.response.error == "Cancelled by user"
    assert runtime.is_generating is False
    assert runtime._generation_tasks_by_request == {}
