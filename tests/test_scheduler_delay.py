from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from orchestrator.scheduler import TaskScheduler


class _FakeRuntime:
    name = "zelda"
    startup_success = True

    def __init__(self):
        self.enqueued: list[dict] = []
        self.transcript: list[tuple[str, str, str]] = []

    async def enqueue_request(self, chat_id, prompt, source, summary, **kwargs):
        request_id = f"req-{len(self.enqueued) + 1}"
        row = {
            "request_id": request_id,
            "chat_id": chat_id,
            "prompt": prompt,
            "source": source,
            "summary": summary,
        }
        row.update(kwargs)
        self.enqueued.append(row)
        return request_id

    def append_conversation_entry(self, role, text, source):
        self.transcript.append((role, text, source))


def _scheduler(tmp_path, *, runtime=None) -> TaskScheduler:
    return TaskScheduler(
        tasks_path=tmp_path / "tasks.json",
        state_path=tmp_path / "scheduler_state.json",
        runtimes=[runtime] if runtime is not None else [],
        authorized_id=123,
    )


@pytest.mark.asyncio
async def test_delay_persists_and_dispatches_through_normal_text_queue_when_due(
    tmp_path,
):
    runtime = _FakeRuntime()
    scheduler = _scheduler(tmp_path, runtime=runtime)

    record = await scheduler.schedule_delayed_message(
        agent_name="zelda",
        chat_id=42,
        prompt="send me a message to say hi",
        delay_minutes=5,
        now_ts=1_000,
    )

    assert record["due_at"] == 1_300
    assert scheduler.count_delayed_messages("zelda") == 1
    saved = json.loads((tmp_path / "scheduler_state.json").read_text(encoding="utf-8"))
    assert (
        saved["delayed_messages"][record["id"]]["prompt"]
        == "send me a message to say hi"
    )

    assert (
        await scheduler.dispatch_due_delayed_messages({"zelda": runtime}, now_ts=1_299)
        == 0
    )
    assert runtime.enqueued == []

    assert (
        await scheduler.dispatch_due_delayed_messages({"zelda": runtime}, now_ts=1_300)
        == 1
    )
    assert runtime.enqueued == [
        {
            "request_id": "req-1",
            "chat_id": 42,
            "prompt": "send me a message to say hi",
            "source": "text",
            "summary": "send me a message to say hi",
        }
    ]
    assert runtime.transcript == [("user", "send me a message to say hi", "text")]
    assert scheduler.count_delayed_messages("zelda") == 0
    saved = json.loads((tmp_path / "scheduler_state.json").read_text(encoding="utf-8"))
    assert saved["delayed_messages"] == {}


@pytest.mark.asyncio
async def test_delay_survives_scheduler_recreation_and_waits_for_offline_agent(
    tmp_path,
):
    first = _scheduler(tmp_path)
    record = await first.schedule_delayed_message(
        agent_name="sunny",
        chat_id=99,
        prompt="check the build",
        delay_minutes=1,
        now_ts=2_000,
    )

    recreated = _scheduler(tmp_path)
    assert [item["id"] for item in await recreated.list_delayed_messages("sunny")] == [
        record["id"]
    ]
    assert await recreated.dispatch_due_delayed_messages({}, now_ts=2_100) == 0
    assert recreated.count_delayed_messages("sunny") == 1

    runtime = _FakeRuntime()
    runtime.name = "sunny"
    assert (
        await recreated.dispatch_due_delayed_messages({"sunny": runtime}, now_ts=2_100)
        == 1
    )
    assert runtime.enqueued[0]["prompt"] == "check the build"


@pytest.mark.asyncio
async def test_delayed_message_preserves_session_route_across_restart(tmp_path):
    metadata = {
        "session_id": "ses-workbench-a",
        "owner_id": "user:123",
        "session_surface": "workbench",
        "session_channel_key": "window-a",
    }
    first = _scheduler(tmp_path)
    await first.schedule_delayed_message(
        agent_name="zelda",
        chat_id=0,
        prompt="continue this Session later",
        delay_minutes=1,
        now_ts=2_000,
        request_metadata=metadata,
        deliver_to_telegram=False,
    )

    recreated = _scheduler(tmp_path)
    runtime = _FakeRuntime()
    await recreated.dispatch_due_delayed_messages({"zelda": runtime}, now_ts=2_100)

    assert runtime.enqueued[0]["request_metadata"] == metadata
    assert runtime.enqueued[0]["deliver_to_telegram"] is False


@pytest.mark.asyncio
async def test_delay_cancel_and_idempotency_are_persistent(tmp_path):
    scheduler = _scheduler(tmp_path)
    first = await scheduler.schedule_delayed_message(
        agent_name="zelda",
        chat_id=42,
        prompt="hello",
        delay_minutes=5,
        idempotency_key="telegram:123",
        now_ts=3_000,
    )
    duplicate = await scheduler.schedule_delayed_message(
        agent_name="zelda",
        chat_id=42,
        prompt="hello",
        delay_minutes=5,
        idempotency_key="telegram:123",
        now_ts=3_001,
    )

    assert duplicate["id"] == first["id"]
    assert duplicate["deduplicated"] is True
    assert scheduler.count_delayed_messages("zelda") == 1

    removed = await scheduler.cancel_delayed_messages(
        "zelda",
        delay_ids={first["id"]},
    )
    assert [item["id"] for item in removed] == [first["id"]]
    assert scheduler.count_delayed_messages("zelda") == 0


@pytest.mark.asyncio
async def test_due_payload_that_starts_with_slash_is_enqueued_as_text(tmp_path):
    runtime = _FakeRuntime()
    scheduler = _scheduler(tmp_path, runtime=runtime)
    await scheduler.schedule_delayed_message(
        agent_name="zelda",
        chat_id=42,
        prompt="/stop",
        delay_minutes=1,
        now_ts=4_000,
    )

    await scheduler.dispatch_due_delayed_messages({"zelda": runtime}, now_ts=4_060)

    assert runtime.enqueued[0]["source"] == "text"
    assert runtime.enqueued[0]["prompt"] == "/stop"


@pytest.mark.asyncio
async def test_failed_enqueue_remains_pending_for_retry(tmp_path):
    scheduler = _scheduler(tmp_path)
    record = await scheduler.schedule_delayed_message(
        agent_name="zelda",
        chat_id=42,
        prompt="retry me",
        delay_minutes=1,
        now_ts=5_000,
    )

    async def _fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("queue unavailable")

    runtime = SimpleNamespace(name="zelda", enqueue_request=_fail_enqueue)
    assert (
        await scheduler.dispatch_due_delayed_messages({"zelda": runtime}, now_ts=5_060)
        == 0
    )

    remaining = await scheduler.list_delayed_messages("zelda")
    assert remaining[0]["id"] == record["id"]
    assert remaining[0]["attempts"] == 1
    assert "queue unavailable" in remaining[0]["last_error"]


@pytest.mark.asyncio
async def test_scheduler_run_tick_dispatches_due_delay(tmp_path):
    runtime = _FakeRuntime()
    scheduler = _scheduler(tmp_path, runtime=runtime)
    await scheduler.schedule_delayed_message(
        agent_name="zelda",
        chat_id=42,
        prompt="dispatch from scheduler loop",
        delay_minutes=1,
        now_ts=time.time() - 120,
    )

    task = asyncio.create_task(scheduler.run())
    for _ in range(20):
        if runtime.enqueued:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runtime.enqueued[0]["prompt"] == "dispatch from scheduler loop"
    assert scheduler.count_delayed_messages("zelda") == 0


@pytest.mark.asyncio
async def test_delay_never_mutates_cron_heartbeat_or_nudge_definitions(tmp_path):
    tasks_path = tmp_path / "tasks.json"
    original = {
        "heartbeats": [{"id": "hb-1", "enabled": True}],
        "crons": [{"id": "cron-1", "enabled": True}],
        "nudges": [{"id": "nudge-1", "enabled": True}],
    }
    tasks_path.write_text(json.dumps(original, indent=2), encoding="utf-8")
    scheduler = _scheduler(tmp_path)

    await scheduler.schedule_delayed_message(
        agent_name="zelda",
        chat_id=42,
        prompt="independent future message",
        delay_minutes=5,
    )

    assert json.loads(tasks_path.read_text(encoding="utf-8")) == original
