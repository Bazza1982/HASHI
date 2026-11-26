"""Worker side of a forced reboot: the stop records what it removes, and only
that work is re-admitted.

The record is written where the removal happens, so a finished request never
looks removed, work enqueued after the arm is seen, and one original request is
never handed back twice.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from orchestrator import reboot_capture, runtime_control, runtime_pending
from orchestrator.agent_stop_fence import current_runtime_epoch, store_for_runtime
from orchestrator.function_worker_host import FunctionWorkerHost
from orchestrator.runtime_common import QueuedRequest


class _Scheduler:
    """Stands in for the Core scheduler the Worker reaches over RPC."""

    def __init__(self, delayed=()) -> None:
        self.delayed = [dict(record) for record in delayed]
        self.restored: list[dict] = []
        self.recomputed: list[dict] = []

    async def list_delayed_messages(self, agent_name):
        return [dict(record) for record in self.delayed]

    async def restore_delayed_message(self, record):
        self.delayed.append(dict(record))
        self.restored.append(dict(record))
        return dict(record)

    async def schedule_delayed_message(self, **kwargs):
        self.recomputed.append(dict(kwargs))
        return {"id": f"delay-{len(self.recomputed)}"}

    async def cancel_delayed_messages(self, agent_name, *, delay_ids=None):
        wanted = {str(value) for value in delay_ids or ()}
        cancelled = [r for r in self.delayed if str(r.get("id")) in wanted]
        self.delayed = [r for r in self.delayed if str(r.get("id")) not in wanted]
        return cancelled


class _Store:
    """Canonical Session/Run rows, answered the way the real store answers."""

    def __init__(self, sessions=None, runs=None) -> None:
        self.sessions = dict(sessions or {})
        self.runs = dict(runs or {})

    def get_run_by_request(self, request_id, *, owner_id=None, agent_id=None):
        run = self.runs.get(request_id)
        if run is None:
            raise KeyError(request_id)
        return dict(run)

    def get_session(
        self, session_id, *, owner_id=None, agent_id=None, include_deleted=True
    ):
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return dict(session)


class _Runtime:
    """Admission that answers to the same durable fence as the real runtime."""

    def __init__(self, bridge_home, *, delayed=(), name="zelda") -> None:
        self.name = name
        self.queue: asyncio.Queue = asyncio.Queue()
        self.default_session_id = "s1"
        self.global_config = SimpleNamespace(bridge_home=str(bridge_home))
        self._agent_stop_epoch = 0
        self.scheduler = _Scheduler(delayed)
        self.orchestrator = SimpleNamespace(scheduler=self.scheduler)
        self.session_store = None
        self.enqueued: list[dict] = []
        self.reject = False
        self._interrupted_tasks: dict = {}
        self._interrupted_task = None

    async def enqueue_request(self, chat_id=None, prompt=None, source=None, summary=None, **kwargs):
        metadata = dict(kwargs.get("request_metadata") or {})
        supplied = int(metadata.get("agent_stop_epoch") or 0)
        if self.reject or supplied < current_runtime_epoch(self):
            # A stale epoch is exactly what real admission refuses.
            return None
        self.enqueued.append(
            {
                "chat_id": chat_id,
                "prompt": prompt,
                "source": source,
                "summary": summary,
                **kwargs,
            }
        )
        return f"rid-{len(self.enqueued)}"


def _host(runtime) -> FunctionWorkerHost:
    # Built the way the real host is: it owns no Scheduler, that lives in Core.
    host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    host.agent_name = "zelda"
    return host


def _queued(prompt: str, *, session_id: str = "s1") -> QueuedRequest:
    return QueuedRequest(
        request_id=f"req-{prompt}",
        chat_id=42,
        prompt=prompt,
        source="telegram",
        summary=prompt[:20],
        created_at="2026-11-26T13:00:00+11:00",
        session_id=session_id,
        request_metadata={"agent_stop_epoch": 0, "owner_id": "42"},
    )


def _advance(bridge_home) -> int:
    store = store_for_runtime(
        SimpleNamespace(
            name="zelda", global_config=SimpleNamespace(bridge_home=str(bridge_home))
        )
    )
    fence = store.advance(agent="zelda", reason="user_stop", source="slash")
    return int(fence.epoch)


def _record(tmp_path, operation_id="op-1", agent="zelda") -> dict:
    path = reboot_capture.capture_path(tmp_path, operation_id, agent)
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------- what a stop keeps


@pytest.mark.asyncio
async def test_the_stop_records_exactly_what_it_removes(tmp_path, monkeypatch):
    runtime = _Runtime(tmp_path)
    runtime.queue.put_nowait(_queued("already-queued"))
    capture = reboot_capture.arm(runtime, "op-1")

    # Enqueued after the arm: a before/after difference would have missed it.
    runtime.queue.put_nowait(_queued("arrived-late"))

    removed = await runtime_pending.recall_pending(runtime, capture=capture)

    assert removed.total == 2
    written = _record(tmp_path)
    assert sorted(entry["prompt"] for entry in written["pending"]) == [
        "already-queued",
        "arrived-late",
    ]


@pytest.mark.asyncio
async def test_the_record_never_truncates_work(tmp_path):
    runtime = _Runtime(tmp_path)
    for index in range(60):
        runtime.queue.put_nowait(_queued(f"item-{index}"))
    capture = reboot_capture.arm(runtime, "op-2")

    await runtime_pending.recall_pending(runtime, capture=capture)

    assert len(_record(tmp_path, "op-2")["pending"]) == 60


@pytest.mark.asyncio
async def test_nothing_is_removed_when_it_cannot_be_recorded(tmp_path, monkeypatch):
    runtime = _Runtime(tmp_path)
    runtime.queue.put_nowait(_queued("must-survive"))
    capture = reboot_capture.arm(runtime, "op-3")

    def broken(*_args, **_kwargs):
        raise OSError("disk is gone")

    monkeypatch.setattr(capture, "_flush", broken)

    removed = await runtime_pending.recall_pending(runtime, capture=capture)

    assert removed.total == 0
    assert runtime.queue.qsize() == 1, "work is only removed once it is on disk"


@pytest.mark.asyncio
async def test_a_real_interrupted_snapshot_is_recorded_verbatim(tmp_path):
    """The stop hands over its own snapshot type, not a dict."""
    from orchestrator.runtime_retry import InterruptedTaskSnapshot

    runtime = _Runtime(tmp_path)
    capture = reboot_capture.arm(runtime, "op-snap")
    snapshot = InterruptedTaskSnapshot(
        prompt="long task",
        chat_id=42,
        source="telegram",
        summary="long task",
        request_id="req-snap",
        request_content={"content": "long task"},
        session_id="s1",
        context_generation=4,
    )

    capture.record_interrupted([snapshot])

    written = _record(tmp_path, "op-snap")["interrupted"][0]
    assert written["prompt"] == "long task"
    assert written["request_id"] == "req-snap"
    assert written["session_id"] == "s1"
    assert written["context_generation"] == 4
    assert written["request_content"] == {"content": "long task"}


@pytest.mark.asyncio
async def test_a_turn_recorded_as_interrupted_is_not_also_queued(tmp_path):
    runtime = _Runtime(tmp_path)
    capture = reboot_capture.arm(runtime, "op-4")
    turn = {
        "prompt": "long task",
        "chat_id": 42,
        "request_id": "req-same",
        "session_id": "s1",
    }
    capture.record_interrupted([turn])
    runtime.queue.put_nowait(
        QueuedRequest(
            request_id="req-same",
            chat_id=42,
            prompt="long task",
            source="telegram",
            summary="long task",
            created_at="2026-11-26T13:00:00+11:00",
            session_id="s1",
        )
    )

    await runtime_pending.recall_pending(runtime, capture=capture)

    written = _record(tmp_path, "op-4")
    assert [entry["request_id"] for entry in written["interrupted"]] == ["req-same"]
    assert written["pending"] == [], "one original request is recorded once"


# -------------------------------------------------------------------- re-admission


@pytest.mark.asyncio
async def test_resume_readmits_after_the_real_stop_epoch_advances(tmp_path):
    runtime = _Runtime(tmp_path)
    capture = reboot_capture.arm(runtime, "op-5")
    runtime.queue.put_nowait(_queued("queued"))
    await runtime_pending.recall_pending(runtime, capture=capture)
    entry = _record(tmp_path, "op-5")["pending"][0]
    assert entry["request_metadata"]["agent_stop_epoch"] == 0

    epoch = _advance(tmp_path)
    assert epoch == 1

    result = await _host(runtime)._resume_after_reboot(
        runtime, {"pending": [entry], "operation_id": "op-5"}
    )

    assert result["pending_resumed"] == 1
    assert result["unconfirmed"] == []
    admission = runtime.enqueued[0]
    assert admission["prompt"] == "queued"
    assert admission["request_metadata"]["agent_stop_epoch"] == 1
    assert admission["request_metadata"]["session_id"] == "s1"
    assert admission["request_metadata"]["owner_id"] == "42"
    assert admission["request_metadata"]["reboot_resume_operation_id"] == "op-5"
    assert admission["idempotency_key"] == "reboot-resume:op-5:req-queued"


@pytest.mark.asyncio
async def test_a_rejected_admission_is_not_counted_as_restored(tmp_path):
    runtime = _Runtime(tmp_path)
    capture = reboot_capture.arm(runtime, "op-6")
    runtime.queue.put_nowait(_queued("queued"))
    await runtime_pending.recall_pending(runtime, capture=capture)
    entry = _record(tmp_path, "op-6")["pending"][0]
    _advance(tmp_path)
    runtime.reject = True

    result = await _host(runtime)._resume_after_reboot(
        runtime, {"pending": [entry], "operation_id": "op-6"}
    )

    assert result["pending_resumed"] == 0
    assert [item["kind"] for item in result["unconfirmed"]] == ["pending"]
    assert runtime.enqueued == []


@pytest.mark.asyncio
async def test_resume_skips_work_that_survived_the_stop(tmp_path):
    runtime = _Runtime(tmp_path)
    capture = reboot_capture.arm(runtime, "op-7")
    runtime.queue.put_nowait(_queued("kept"))
    await runtime_pending.recall_pending(runtime, capture=capture)
    entry = _record(tmp_path, "op-7")["pending"][0]
    runtime.queue.put_nowait(_queued("kept"))  # admitted again after the stop

    result = await _host(runtime)._resume_after_reboot(
        runtime, {"pending": [entry], "operation_id": "op-7"}
    )

    assert result["pending_resumed"] == 0
    assert runtime.enqueued == []


@pytest.mark.asyncio
async def test_a_request_that_finished_during_the_stop_is_never_rerun(tmp_path):
    """Regression: a completed request is not removed work."""
    runtime = _Runtime(tmp_path)
    runtime.session_store = _Store(
        sessions={"s1": {"session_id": "s1", "owner_id": "42", "context_generation": 3}},
        runs={
            "req-done": {
                "request_id": "req-done",
                "session_id": "s1",
                "state": "completed",
                "context_generation": 3,
            }
        },
    )

    entry = {
        "request_id": "req-done",
        "chat_id": 42,
        "prompt": "finished while stopping",
        "session_id": "s1",
    }
    result = await _host(runtime)._resume_after_reboot(
        runtime, {"pending": [entry], "operation_id": "op-8"}
    )

    assert result["pending_resumed"] == 0
    assert result["unconfirmed"][0]["reason"] == "already_finished"
    assert runtime.enqueued == []


@pytest.mark.asyncio
async def test_a_turn_keeps_its_original_owner_and_generation(tmp_path):
    runtime = _Runtime(tmp_path)
    route = {
        "surface": "telegram",
        "channel_key": "42",
        "chat_id": 42,
        "thread_id": 7,
    }
    runtime.session_store = _Store(
        sessions={"s1": {"session_id": "s1", "owner_id": "user:7", "context_generation": 5}},
        runs={
            "req-9": {
                "request_id": "req-9",
                "session_id": "s1",
                "state": "stopped",
                "context_generation": 5,
                "delivery_route": route,
            }
        },
    )
    turn = {
        "prompt": "long task",
        "chat_id": 42,
        "source": "telegram",
        "summary": "long task",
        "request_id": "req-9",
        "session_id": "s1",
        "request_content": [{"type": "text", "text": "long task"}],
    }

    result = await _host(runtime)._resume_after_reboot(
        runtime, {"interrupted": [turn], "operation_id": "op-9"}
    )

    assert result["turns_resumed"] == 1
    admission = runtime.enqueued[0]
    metadata = admission["request_metadata"]
    assert metadata["owner_id"] == "user:7"
    assert metadata["session_context_generation"] == 5
    assert metadata["session_id"] == "s1"
    assert metadata["resumed_interrupted_task"] == turn
    assert admission["request_content"] == turn["request_content"]
    # Admission rebuilds the delivery route from this channel, so the original
    # channel is carried, not a route object it would ignore.
    assert metadata["session_surface"] == "telegram"
    assert metadata["session_channel_key"] == "42"
    assert "_run_delivery_route" not in metadata


@pytest.mark.asyncio
async def test_a_newer_context_generation_is_never_used_silently(tmp_path):
    runtime = _Runtime(tmp_path)
    runtime.session_store = _Store(
        sessions={"s1": {"session_id": "s1", "owner_id": "42", "context_generation": 6}},
        runs={
            "req-10": {
                "request_id": "req-10",
                "session_id": "s1",
                "state": "stopped",
                "context_generation": 5,
            }
        },
    )
    turn = {
        "prompt": "long task",
        "chat_id": 42,
        "request_id": "req-10",
        "session_id": "s1",
    }

    result = await _host(runtime)._resume_after_reboot(
        runtime, {"interrupted": [turn], "operation_id": "op-10"}
    )

    assert result["turns_resumed"] == 0
    assert result["unconfirmed"][0]["reason"] == "context_generation_changed"
    assert runtime.enqueued == []


@pytest.mark.asyncio
async def test_one_original_request_is_restored_once(tmp_path):
    """Regression: the same request must not run as queued work and as a turn."""
    runtime = _Runtime(tmp_path)
    turn = {
        "prompt": "long task",
        "chat_id": 42,
        "request_id": "req-11",
        "session_id": "s1",
    }
    queued = {
        "request_id": "req-11",
        "chat_id": 42,
        "prompt": "long task",
        "session_id": "s1",
    }

    result = await _host(runtime)._resume_after_reboot(
        runtime,
        {"pending": [queued], "interrupted": [turn], "operation_id": "op-11"},
    )

    assert result["pending_resumed"] + result["turns_resumed"] == 1
    assert len(runtime.enqueued) == 1


@pytest.mark.asyncio
async def test_a_turn_outside_the_record_is_never_revived(tmp_path):
    runtime = _Runtime(tmp_path)
    runtime._interrupted_tasks["s1"] = SimpleNamespace(
        prompt="stopped by the user yesterday",
        chat_id=42,
        source="telegram",
        summary="old",
        request_id="req-old",
        session_id="s1",
    )

    result = await _host(runtime)._resume_after_reboot(
        runtime, {"pending": [], "interrupted": [], "operation_id": "op-12"}
    )

    assert result["turn_resumed"] is False
    assert runtime.enqueued == []


@pytest.mark.asyncio
async def test_resume_works_without_a_worker_scheduler(tmp_path):
    # The real FunctionWorkerHost owns no Scheduler: delayed messages belong to
    # Core, so a resume of queued work must not reach for one.
    runtime = _Runtime(tmp_path)
    capture = reboot_capture.arm(runtime, "op-13")
    runtime.queue.put_nowait(_queued("queued"))
    await runtime_pending.recall_pending(runtime, capture=capture)
    entry = _record(tmp_path, "op-13")["pending"][0]
    _advance(tmp_path)

    host = _host(runtime)
    assert not hasattr(host, "scheduler")

    result = await host._resume_after_reboot(
        runtime, {"pending": [entry], "operation_id": "op-13"}
    )

    assert result["pending_resumed"] == 1
    assert "delayed_resumed" not in result
    assert runtime.enqueued[0]["prompt"] == "queued"


@pytest.mark.asyncio
async def test_arming_alone_writes_no_record(tmp_path):
    runtime = _Runtime(tmp_path)
    capture = reboot_capture.arm(runtime, "op-15")
    assert reboot_capture.armed(runtime) is capture
    assert not capture.path.exists(), "an armed capture is not evidence yet"

    reboot_capture.complete(runtime, capture)

    payload = json.loads(capture.path.read_text(encoding="utf-8"))
    assert payload["phase"] == "stopped"
    assert payload["pending"] == []
    assert reboot_capture.is_complete(payload)
    assert reboot_capture.armed(runtime) is None, "a finished op cannot arm later stops"


@pytest.mark.asyncio
async def test_a_stale_record_stops_arming_without_spinning(tmp_path):
    runtime = _Runtime(tmp_path)
    capture = reboot_capture.arm(runtime, "op-18")
    capture.armed_at -= reboot_capture.CAPTURE_MAX_AGE_SECONDS + 1

    assert reboot_capture.armed(runtime) is None
    assert reboot_capture.armed(runtime, "op-18") is None


@pytest.mark.asyncio
async def test_an_aborted_operation_leaves_a_normal_stop_alone(tmp_path):
    runtime = _Runtime(tmp_path)
    capture = reboot_capture.arm(runtime, "op-16")
    reboot_capture.disarm(runtime, "op-16")
    assert reboot_capture.armed(runtime) is None

    runtime.queue.put_nowait(_queued("normal"))
    await runtime_pending.recall_pending(runtime)

    assert runtime.queue.qsize() == 0, "the ordinary stop still clears the queue"
    assert not capture.path.exists(), "a normal stop records nothing"


@pytest.mark.asyncio
async def test_a_record_that_could_not_be_written_says_so(tmp_path, monkeypatch):
    runtime = _Runtime(tmp_path)
    capture = reboot_capture.arm(runtime, "op-17")

    def broken(*_args, **_kwargs):
        raise OSError("disk is gone")

    monkeypatch.setattr(capture, "_flush", broken)
    with pytest.raises(OSError):
        capture.record_interrupted([{"request_id": "req-1", "prompt": "long task"}])

    assert capture.broken is True
    reboot_capture.complete(runtime, capture)
    assert capture.phase == "incomplete"
    assert reboot_capture.is_complete(capture.payload()) is False

def _stop_runtime(bridge_home):
    """The stop path as the Worker runs it, with a durable home for its record."""

    async def _reply(_update, _text, **_kwargs):
        return None

    return SimpleNamespace(
        name="zelda",
        global_config=SimpleNamespace(bridge_home=str(bridge_home), authorized_id=42),
        config=SimpleNamespace(active_backend="her-v2", engine="her-v2"),
        logger=SimpleNamespace(
            warning=lambda *a, **k: None, info=lambda *a, **k: None
        ),
        queue=asyncio.Queue(),
        backend_manager=SimpleNamespace(current_backend=None),
        orchestrator=SimpleNamespace(scheduler=None),
        current_request_meta={
            "request_id": "req-busy",
            "session_id": "s1",
            "chat_id": 42,
            "prompt": "long task",
            "source": "telegram",
            "summary": "long task",
        },
        last_prompt=None,
        is_generating=True,
        _is_authorized_user=lambda _user_id: True,
        _reply_text=_reply,
    )


def _stop_update():
    message = SimpleNamespace(text="/stop", chat=SimpleNamespace(id=42))
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42),
        effective_message=message,
        message=message,
    )


@pytest.mark.asyncio
async def test_a_user_stop_never_consumes_an_armed_force_capture(tmp_path):
    """Regression: /stop typed between arm and force must read nothing at all."""
    runtime = _stop_runtime(tmp_path)
    capture = reboot_capture.arm(runtime, "op-20")

    await runtime_control.cmd_stop(runtime, _stop_update(), SimpleNamespace(args=[]))

    assert reboot_capture.armed(runtime) is capture, "the force record stays armed"
    assert capture.completed is False, "an ordinary stop never finishes it"
    assert not capture.path.exists(), "an ordinary stop writes nothing into it"
    assert capture.interrupted == [] and capture.pending == []


@pytest.mark.asyncio
async def test_only_the_operation_that_armed_the_capture_can_fill_it(tmp_path):
    runtime = _stop_runtime(tmp_path)
    capture = reboot_capture.arm(runtime, "op-21")

    await runtime_control.cmd_stop(
        runtime,
        _stop_update(),
        SimpleNamespace(
            args=[],
            source_channel="reboot",
            _hashi_reboot_operation_id="op-other",
        ),
    )

    assert reboot_capture.armed(runtime) is capture
    assert not capture.path.exists(), "another operation's stop cannot claim it"

    await runtime_control.cmd_stop(
        runtime,
        _stop_update(),
        SimpleNamespace(
            args=[],
            source_channel="reboot",
            _hashi_reboot_operation_id="op-21",
        ),
    )

    payload = json.loads(capture.path.read_text(encoding="utf-8"))
    assert payload["phase"] == "stopped"
    assert [item["request_id"] for item in payload["interrupted"]] == ["req-busy"]
    assert reboot_capture.armed(runtime) is None


@pytest.mark.asyncio
async def test_the_force_stop_carries_its_operation_to_the_worker(tmp_path, monkeypatch):
    """The bound id has to travel with the stop command, not be guessed."""
    from tests.test_reboot_force import _setup, _write_capture

    kernel, manager, request = _setup(tmp_path, monkeypatch)
    handle = kernel.runtimes[0]
    handle.client.metadata["is_generating"] = True
    seen: list[dict] = []

    async def arm(operation_id, **_kwargs):
        return {"armed": True, "operation_id": str(operation_id), "durable": True}

    async def execute(text, *, source_channel, session_metadata=None, **_kwargs):
        operation_id = str((session_metadata or {}).get("_hashi_reboot_operation_id"))
        seen.append(
            {
                "text": text,
                "source_channel": source_channel,
                "operation_id": operation_id,
            }
        )
        handle.client.metadata["is_generating"] = False
        handle.client.metadata["queue_depth"] = 0
        _write_capture(tmp_path, operation_id, handle.name, {})
        return {"ok": True}

    handle.arm_reboot_capture = arm
    handle.execute_slash_command = execute

    accepted = manager.submit({**request, "force": True})
    await asyncio.sleep(0.25)

    operation_id = accepted["record"]["id"]
    assert seen and seen[0]["text"] == "/stop"
    assert seen[0]["operation_id"] == operation_id
# HASHI-Fingerprint: hfp_14ad2738c68841b893afa64e3b1c853e
