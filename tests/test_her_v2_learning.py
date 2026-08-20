from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from adapters.her_habits import HabitMeditationConfig
from orchestrator.her_v2.audit import DurableAuditLog
from orchestrator.her_v2.learning import HERv2Learning
from orchestrator.her_v2.models import Stage, StageResponse, TerminalState


class ScriptedMaintenance:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(
        self, stage, prompt, turn_id, request_id, timeout_s
    ) -> StageResponse:
        self.calls.append(
            {
                "stage": stage,
                "prompt": prompt,
                "turn_id": turn_id,
                "request_id": request_id,
                "timeout_s": timeout_s,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected maintenance model invocation")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return StageResponse(
            text=value,
            provider="fake-api",
            model="configured-learning-model",
            reasoning_trace="bounded learning trace",
        )


def _service(tmp_path, invoker, *, enabled=True):
    root = tmp_path / "workspace"
    audit = DurableAuditLog(
        root / "logs" / "her_v2_audit.jsonl",
        root / "backend_state" / "her_v2" / "audit_fallback.jsonl",
    )
    config = HabitMeditationConfig(enabled=enabled, max_actions=3)
    service = HERv2Learning(
        workspace_dir=root,
        agent_name="agent",
        config_getter=lambda: config,
        invoke_model=invoker,
        audit_log=audit,
    )
    return service, root


async def _wait_for_learning(service: HERv2Learning) -> None:
    for _ in range(200):
        tasks = [task for task in service.meditation_tasks if not task.done()]
        if not tasks and not service.meditation_job_ids:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("HER v2 learning task did not become idle")


@pytest.mark.asyncio
async def test_meditation_persists_validated_habit_and_full_audit(tmp_path):
    invoker = ScriptedMaintenance(
        [
            json.dumps(
                {
                    "actions": [
                        {
                            "operation": "create",
                            "title": "Verify current state",
                            "metadata": "Use when external state may have changed.",
                            "body": "Inspect current state before applying another change.",
                        }
                    ]
                }
            )
        ]
    )
    service, root = _service(tmp_path, invoker)
    learning = service.bind_turn()

    await learning.meditate(
        turn_id="turn-learning-1",
        goal="Update the external setting safely",
        summary="The update completed after checking the current state.",
        evidence_refs=("receipt:123",),
        limitations=(),
        terminal_state=TerminalState.COMPLETED,
    )
    await _wait_for_learning(service)

    habits = service.store.load()
    assert [habit.title for habit in habits] == ["Verify current state"]
    assert invoker.calls[0]["stage"] is Stage.MEDITATION
    assert "HER HABIT MEDITATION" in invoker.calls[0]["prompt"]
    jobs = list(service.meditation_journal.root.glob("*.json"))
    assert len(jobs) == 1
    job = json.loads(jobs[0].read_text(encoding="utf-8"))
    assert job["status"] == "completed"
    assert job["changes"][0]["operation"] == "created"

    rows = [
        json.loads(line)
        for line in (root / "logs" / "her_v2_audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    events = {row["event"] for row in rows}
    assert {
        "meditation_enqueue_intent",
        "meditation_enqueued",
        "reasoning_trace",
        "habit_write_authorised",
        "meditation_completed",
    } <= events
    reasoning = next(row for row in rows if row["event"] == "reasoning_trace")
    assert reasoning["payload"]["availability"] == "available"


@pytest.mark.asyncio
async def test_restart_recovers_running_meditation_without_losing_job(tmp_path):
    invoker = ScriptedMaintenance(
        ['{"actions":[{"operation":"create","title":"Recover safely",'
         '"metadata":"Use after interrupted background learning.",'
         '"body":"Resume the durable job without repeating the user turn."}]}']
    )
    service, _root = _service(tmp_path, invoker)
    turn_id = "turn-interrupted-learning"
    job_id = hashlib.sha256(
        f"her-v2-meditation\0{turn_id}".encode("utf-8")
    ).hexdigest()[:32]
    service.meditation_journal.enqueue(
        job_id=job_id,
        request_id=turn_id,
        prompt="HER HABIT MEDITATION — recover this durable job",
        max_actions=3,
    )
    assert service.meditation_journal.claim(job_id) == "meditate"
    assert service.meditation_journal.get(job_id)["status"] == "running"

    recovery = service.recover()
    await _wait_for_learning(service)

    assert recovery.interrupted_meditations == 1
    assert recovery.resumed_meditations == 1
    assert service.meditation_journal.get(job_id)["status"] == "completed"
    assert [habit.title for habit in service.store.load()] == ["Recover safely"]


@pytest.mark.asyncio
async def test_applying_recovery_replays_durable_actions_without_model_call(tmp_path):
    invoker = ScriptedMaintenance([])
    service, _root = _service(tmp_path, invoker)
    job_id = "a" * 32
    turn_id = "turn-apply-recovery"
    actions = [
        {
            "operation": "create",
            "title": "Apply once",
            "metadata": "Use when a durable write phase resumes.",
            "body": "Replay the journalled action with its idempotency key.",
        }
    ]
    service.meditation_journal.enqueue(
        job_id=job_id,
        request_id=turn_id,
        prompt="unused",
        max_actions=3,
    )
    assert service.meditation_journal.claim(job_id) == "meditate"
    baseline = service.store.capture_action_baseline(
        actions, max_actions=3, idempotency_key=job_id
    )
    service.meditation_journal.store_actions(
        job_id, actions, action_baseline=baseline
    )

    recovery = service.recover()
    await _wait_for_learning(service)

    assert recovery.resumed_meditations == 1
    assert invoker.calls == []
    assert service.meditation_journal.get(job_id)["status"] == "completed"
    assert [habit.title for habit in service.store.load()] == ["Apply once"]


@pytest.mark.asyncio
async def test_habit_retrieval_is_disabled_without_reading_catalogue(tmp_path, monkeypatch):
    service, _root = _service(tmp_path, ScriptedMaintenance([]), enabled=False)

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("disabled HER v2 learning must not inspect Habit files")

    monkeypatch.setattr(service.store, "retrieve", forbidden_read)
    assert await service.retrieve(goal="anything", turn_id="turn-disabled") == ()
