from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator import runtime_cancel, runtime_pipeline


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
