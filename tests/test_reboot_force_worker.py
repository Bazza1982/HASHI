"""Worker side of a forced reboot: capture before the stop, resume after it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator.function_worker_host import FunctionWorkerHost
from orchestrator.runtime_common import QueuedRequest


class _Runtime:
    def __init__(self) -> None:
        self.name = "zelda"
        self.queue: asyncio.Queue = asyncio.Queue()
        self.default_session_id = "s1"
        self.enqueued: list[dict] = []
        self._interrupted_tasks: dict = {}
        self._interrupted_task = None

    async def enqueue_request(self, chat_id, prompt, source, summary, **kwargs):
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


def _host() -> FunctionWorkerHost:
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
    )


def test_capture_reads_the_ready_queue_without_touching_it():
    runtime = _Runtime()
    first, second = _queued("one"), _queued("two")
    runtime.queue.put_nowait(first)
    runtime.queue.put_nowait(second)

    snapshot = _host()._capture_reboot_state(runtime)

    assert snapshot["pending_total"] == 2
    assert [entry["prompt"] for entry in snapshot["pending"]] == ["one", "two"]
    assert snapshot["pending"][0]["chat_id"] == 42
    assert snapshot["interrupted"] is False
    assert runtime.queue.qsize() == 2


@pytest.mark.asyncio
async def test_resume_requeues_captured_requests_and_the_interrupted_turn():
    runtime = _Runtime()
    host = _host()
    snapshot = {
        "prompt": "long task",
        "chat_id": 42,
        "source": "telegram",
        "summary": "long task",
        "request_id": "req-9",
        "session_id": "s1",
    }
    runtime._interrupted_tasks["s1"] = SimpleNamespace(**snapshot)

    result = await host._resume_after_reboot(
        runtime,
        {
            "pending": [{"chat_id": 42, "prompt": "queued", "source": "api", "summary": ""}],
            "resume_interrupted": True,
            "operation_id": "op-1",
        },
    )

    assert result == {"pending_resumed": 1, "pending_total": 1, "turn_resumed": True}
    assert runtime.enqueued[0]["prompt"] == "queued"
    assert runtime.enqueued[1]["prompt"] == "long task"
    assert runtime.enqueued[1]["request_metadata"]["resumed_interrupted_task"]
    assert runtime.enqueued[1]["idempotency_key"].startswith("reboot-resume:op-1:")


@pytest.mark.asyncio
async def test_resume_without_an_interrupted_turn_does_not_invent_one():
    runtime = _Runtime()

    result = await _host()._resume_after_reboot(
        runtime, {"pending": [], "resume_interrupted": True, "operation_id": "op-2"}
    )

    assert result["turn_resumed"] is False
    assert runtime.enqueued == []
