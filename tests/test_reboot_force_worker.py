"""Worker side of a forced reboot: capture, then re-admit only what was removed."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator.agent_stop_fence import current_runtime_epoch, store_for_runtime
from orchestrator.function_worker_host import FunctionWorkerHost
from orchestrator.runtime_common import QueuedRequest


class _Scheduler:
    """Stands in for the Core scheduler the Worker reaches over RPC."""

    def __init__(self, delayed=()) -> None:
        self.delayed = [dict(record) for record in delayed]
        self.scheduled: list[dict] = []

    async def list_delayed_messages(self, agent_name):
        return [dict(record) for record in self.delayed]

    async def schedule_delayed_message(self, **kwargs):
        self.scheduled.append(dict(kwargs))
        return {"id": f"delay-{len(self.scheduled)}"}


class _Runtime:
    """Admission that answers to the same durable fence as the real runtime."""

    def __init__(self, bridge_home, *, delayed=(), name="zelda") -> None:
        self.name = name
        self.queue: asyncio.Queue = asyncio.Queue()
        self.default_session_id = "s1"
        self.global_config = SimpleNamespace(bridge_home=str(bridge_home))
        self._agent_stop_epoch = 0
        self.scheduler = _Scheduler(delayed)
        self.enqueued: list[dict] = []
        self.reject = False
        self._interrupted_tasks: dict = {}
        self._interrupted_task = None

    async def enqueue_request(self, chat_id, prompt, source, summary, **kwargs):
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
    host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    host.agent_name = "zelda"
    host.scheduler = runtime.scheduler
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


@pytest.mark.asyncio
async def test_capture_reads_the_ready_queue_without_touching_it(tmp_path):
    runtime = _Runtime(tmp_path)
    first, second = _queued("one"), _queued("two")
    runtime.queue.put_nowait(first)
    runtime.queue.put_nowait(second)

    snapshot = await _host(runtime)._capture_reboot_state(runtime)

    assert snapshot["pending_total"] == 2
    assert [entry["prompt"] for entry in snapshot["pending"]] == ["one", "two"]
    assert snapshot["pending"][0]["chat_id"] == 42
    assert snapshot["pending"][0]["request_id"] == "req-one"
    assert snapshot["pending"][0]["session_id"] == "s1"
    assert snapshot["delayed"] == []
    assert snapshot["interrupted"] == []
    assert runtime.queue.qsize() == 2


@pytest.mark.asyncio
async def test_capture_keeps_every_queued_request(tmp_path):
    runtime = _Runtime(tmp_path)
    for index in range(60):
        runtime.queue.put_nowait(_queued(f"item-{index}"))

    snapshot = await _host(runtime)._capture_reboot_state(runtime)

    assert snapshot["pending_total"] == 60
    assert len(snapshot["pending"]) == 60, "the manifest must not truncate work"


@pytest.mark.asyncio
async def test_capture_lists_delayed_messages(tmp_path):
    runtime = _Runtime(
        tmp_path,
        delayed=[
            {
                "id": "d1",
                "chat_id": 42,
                "prompt": "later",
                "due_at": 9_999_999_999.0,
                "request_metadata": {"owner_id": "42"},
            }
        ],
    )

    snapshot = await _host(runtime)._capture_reboot_state(runtime)

    assert [entry["id"] for entry in snapshot["delayed"]] == ["d1"]


@pytest.mark.asyncio
async def test_resume_readmits_after_the_real_stop_epoch_advances(tmp_path):
    runtime = _Runtime(tmp_path)
    runtime.queue.put_nowait(_queued("queued"))
    snapshot = await _host(runtime)._capture_reboot_state(runtime)
    captured = snapshot["pending"]
    assert captured[0]["request_metadata"]["agent_stop_epoch"] == 0

    runtime.queue.get_nowait()  # the /stop removed it
    epoch = _advance(tmp_path)
    assert epoch == 1

    result = await _host(runtime)._resume_after_reboot(
        runtime, {"pending": captured, "operation_id": "op-1"}
    )

    assert result["pending_resumed"] == 1
    assert result["unconfirmed"] == []
    admission = runtime.enqueued[0]
    assert admission["prompt"] == "queued"
    assert admission["request_metadata"]["agent_stop_epoch"] == 1
    assert admission["request_metadata"]["session_id"] == "s1"
    assert admission["request_metadata"]["reboot_resume_operation_id"] == "op-1"
    assert admission["idempotency_key"] == "reboot-resume:op-1:req-queued"


@pytest.mark.asyncio
async def test_a_rejected_admission_is_not_counted_as_restored(tmp_path):
    runtime = _Runtime(tmp_path)
    runtime.queue.put_nowait(_queued("queued"))
    snapshot = await _host(runtime)._capture_reboot_state(runtime)
    runtime.queue.get_nowait()
    _advance(tmp_path)
    runtime.reject = True

    result = await _host(runtime)._resume_after_reboot(
        runtime, {"pending": snapshot["pending"], "operation_id": "op-2"}
    )

    assert result["pending_resumed"] == 0
    assert [item["kind"] for item in result["unconfirmed"]] == ["pending"]
    assert result["unconfirmed"][0]["entry"]["request_id"] == "req-queued"
    assert runtime.enqueued == []


@pytest.mark.asyncio
async def test_resume_skips_work_that_survived_the_stop(tmp_path):
    runtime = _Runtime(tmp_path)
    runtime.queue.put_nowait(_queued("kept"))
    snapshot = await _host(runtime)._capture_reboot_state(runtime)

    result = await _host(runtime)._resume_after_reboot(
        runtime, {"pending": snapshot["pending"], "operation_id": "op-3"}
    )

    assert result["pending_resumed"] == 0
    assert runtime.enqueued == []


@pytest.mark.asyncio
async def test_resume_restores_a_turn_by_its_recorded_identity(tmp_path):
    runtime = _Runtime(tmp_path)
    turn = {
        "prompt": "long task",
        "chat_id": 42,
        "source": "telegram",
        "summary": "long task",
        "request_id": "req-9",
        "session_id": "s1",
        "context_generation": 4,
        "request_content": [{"type": "text", "text": "long task"}],
    }

    result = await _host(runtime)._resume_after_reboot(
        runtime, {"interrupted": [turn], "operation_id": "op-4"}
    )

    assert result["turns_resumed"] == 1
    admission = runtime.enqueued[0]
    assert admission["prompt"] == "long task"
    assert admission["request_metadata"]["session_id"] == "s1"
    assert admission["request_metadata"]["resumed_interrupted_task"] == turn
    assert admission["request_content"] == turn["request_content"]
    assert admission["idempotency_key"] == "reboot-resume:op-4:turn:req-9"


@pytest.mark.asyncio
async def test_resume_never_resurrects_a_turn_outside_the_manifest(tmp_path):
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
        runtime, {"pending": [], "interrupted": [], "operation_id": "op-5"}
    )

    assert result["turn_resumed"] is False
    assert runtime.enqueued == []


@pytest.mark.asyncio
async def test_resume_restores_delayed_messages_with_the_remaining_delay(tmp_path):
    runtime = _Runtime(tmp_path)
    runtime.scheduler.delayed = [
        {
            "id": "d1",
            "chat_id": 42,
            "prompt": "later",
            "due_at": __import__("time").time() + 600,
            "deliver_to_telegram": True,
        }
    ]

    captured = [dict(record) for record in runtime.scheduler.delayed]
    runtime.scheduler.delayed = []  # the /stop cancelled it

    result = await _host(runtime)._resume_after_reboot(
        runtime, {"delayed": captured, "operation_id": "op-6"}
    )

    assert result["delayed_resumed"] == 1
    scheduled = runtime.scheduler.scheduled[0]
    assert scheduled["prompt"] == "later"
    assert scheduled["delay_minutes"] == 10
    assert scheduled["request_metadata"]["reboot_resume_delay_id"] == "d1"
    assert scheduled["idempotency_key"] == "reboot-resume:op-6:delay:d1"


@pytest.mark.asyncio
async def test_resume_does_not_schedule_a_delay_twice(tmp_path):
    runtime = _Runtime(tmp_path)
    runtime.scheduler.delayed = [
        {
            "id": "d1",
            "chat_id": 42,
            "prompt": "later",
            "due_at": __import__("time").time() + 600,
            "request_metadata": {"reboot_resume_delay_id": "d1"},
        }
    ]

    result = await _host(runtime)._resume_after_reboot(
        runtime,
        {
            "delayed": [
                {
                    "id": "d1",
                    "chat_id": 42,
                    "prompt": "later",
                    "due_at": __import__("time").time() + 600,
                }
            ],
            "operation_id": "op-7",
        },
    )

    assert result["delayed_resumed"] == 0
    assert runtime.scheduler.scheduled == []
