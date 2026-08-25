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
        tasks = [
            task
            for task in (
                *service.meditation_tasks,
                *getattr(service, "meter_notification_tasks", set()),
            )
            if not task.done()
        ]
        if (
            not tasks
            and not service.meditation_job_ids
            and not getattr(service, "meter_notification_job_ids", set())
        ):
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
    meditation_input = json.loads(invoker.calls[0]["prompt"])
    assert meditation_input["mode"] == "initial"
    assert meditation_input["agent_name"] == "agent"
    assert meditation_input["max_actions"] == 3
    assert meditation_input["current_user_request"] == (
        "Update the external setting safely"
    )
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
async def test_meditation_format_repair_uses_json_repair_specialist(tmp_path):
    invoker = ScriptedMaintenance(["not-json", '{"actions":[]}'])
    service, _root = _service(tmp_path, invoker)

    await service.bind_turn().meditate(
        turn_id="turn-learning-repair",
        goal="Complete the current request safely.",
        summary="The request completed.",
        evidence_refs=(),
        limitations=(),
        terminal_state=TerminalState.COMPLETED,
    )
    await _wait_for_learning(service)

    assert len(invoker.calls) == 2
    initial_input = json.loads(invoker.calls[0]["prompt"])
    correction_input = json.loads(invoker.calls[1]["prompt"])
    assert initial_input["mode"] == "initial"
    assert invoker.calls[0]["stage"] is Stage.MEDITATION
    assert invoker.calls[1]["stage"] is Stage.JSON_REPAIR
    assert set(correction_input) == {
        "rejected_output",
        "required_schema",
        "validation_error",
    }
    assert correction_input["rejected_output"] == "not-json"
    assert correction_input["validation_error"] == (
        "Meditation response must be one JSON object"
    )
    assert correction_input["required_schema"]["actions"]["max_items"] == 3


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

    monkeypatch.setattr(service.store, "load", forbidden_read)
    assert await service.retrieve(goal="anything", turn_id="turn-disabled") == ()
    assert not (_root / "habits").exists()
    assert not service.meditation_journal.root.exists()


@pytest.mark.asyncio
async def test_planning_receives_all_active_habits_for_semantic_selection(tmp_path):
    service, _root = _service(tmp_path, ScriptedMaintenance([]))
    outcomes = service.store.apply_actions(
        [
            {
                "operation": "create",
                "title": "Remember amberquartz context",
                "metadata": "Relevant only when amberquartz appears in the current task.",
                "body": "This background-only Habit must not enter the current plan.",
            },
            {
                "operation": "create",
                "title": "Handle violetcinder requests",
                "metadata": "Relevant when violetcinder appears in the current task.",
                "body": "Use the bounded current-turn procedure.",
            },
        ],
        max_actions=2,
    )
    background_id, current_id = [item.split(":", 1)[1] for item in outcomes]

    advisory = await service.retrieve(
        goal=(
            "Bridge-managed conversation background mentions amberquartz.\n"
            "--- CURRENT USER REQUEST — AUTHORITATIVE ---\n"
            "Please handle the violetcinder request."
        ),
        turn_id="turn-current-request-only",
    )

    assert len(advisory) == 1
    assert current_id in advisory[0]
    assert background_id in advisory[0]
    assert "violetcinder" in advisory[0]
    assert "amberquartz" in advisory[0]


@pytest.mark.asyncio
async def test_meditation_persists_only_the_current_turn_request(tmp_path):
    invoker = ScriptedMaintenance(['{"actions":[]}'])
    service, _root = _service(tmp_path, invoker)

    await service.bind_turn().meditate(
        turn_id="turn-no-bridge-leak",
        goal=(
            "Bridge background canary AMBERQUARTZ-CONTEXT.\n"
            "--- CURRENT USER REQUEST — AUTHORITATIVE ---\n"
            "Complete the VIOLETCINDER-TURN request."
        ),
        summary="The current request completed.",
        evidence_refs=("receipt:current",),
        limitations=(),
        terminal_state=TerminalState.COMPLETED,
    )
    await _wait_for_learning(service)

    assert len(invoker.calls) == 1
    meditation_input = json.loads(invoker.calls[0]["prompt"])
    assert "VIOLETCINDER-TURN" in meditation_input["current_user_request"]
    assert "AMBERQUARTZ-CONTEXT" not in meditation_input["current_user_request"]
    [job_path] = list(service.meditation_journal.root.glob("*.json"))
    job = json.loads(job_path.read_text(encoding="utf-8"))
    persisted_input = json.loads(job["prompt"])
    assert "VIOLETCINDER-TURN" in persisted_input["current_user_request"]
    assert "AMBERQUARTZ-CONTEXT" not in persisted_input["current_user_request"]
    assert job["status"] == "no_change"


@pytest.mark.asyncio
async def test_duplicate_turn_meditation_is_one_model_decision_and_one_write(tmp_path):
    invoker = ScriptedMaintenance(
        [
            json.dumps(
                {
                    "actions": [
                        {
                            "operation": "create",
                            "title": "Keep one durable decision",
                            "metadata": "Use when a turn callback is delivered twice.",
                            "body": "Reuse the turn job identity instead of learning twice.",
                        }
                    ]
                }
            )
        ]
    )
    service, _root = _service(tmp_path, invoker)
    learning = service.bind_turn()
    call = {
        "turn_id": "turn-idempotent-learning",
        "goal": "Complete one turn",
        "summary": "Completed once.",
        "evidence_refs": ("receipt:one",),
        "limitations": (),
        "terminal_state": TerminalState.COMPLETED,
    }

    await asyncio.gather(learning.meditate(**call), learning.meditate(**call))
    await _wait_for_learning(service)

    assert len(invoker.calls) == 1
    assert len(list(service.meditation_journal.root.glob("*.json"))) == 1
    assert [habit.title for habit in service.store.load()] == [
        "Keep one durable decision"
    ]


@pytest.mark.asyncio
async def test_ineligible_turn_neither_reads_habits_nor_queues_meditation(
    tmp_path, monkeypatch
):
    invoker = ScriptedMaintenance([])
    service, _root = _service(tmp_path, invoker)
    learning = service.bind_turn(learning_eligible=False)

    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("ineligible turn must not inspect Habit files")

    monkeypatch.setattr(service.store, "retrieve", forbidden_read)
    assert await learning.retrieve(goal="anything", turn_id="turn-ineligible") == ()
    await learning.meditate(
        turn_id="turn-ineligible",
        goal="anything",
        summary="unused",
        evidence_refs=(),
        limitations=(),
        terminal_state=TerminalState.COMPLETED,
    )
    await asyncio.sleep(0)

    assert invoker.calls == []
    assert not service.meditation_journal.root.exists()


@pytest.mark.asyncio
async def test_meditation_cost_sender_receives_meter_line_items(tmp_path):
    """After Meditation completes, the injected cost sender receives the job
    with per-invocation line items, gated on the request-bound context."""
    invoker = ScriptedMaintenance(['{"actions":[]}'])
    root = tmp_path / "workspace"
    audit = DurableAuditLog(
        root / "logs" / "her_v2_audit.jsonl",
        root / "backend_state" / "her_v2" / "audit_fallback.jsonl",
    )
    config = HabitMeditationConfig(enabled=True, max_actions=3)
    delivered: list[dict] = []

    async def cost_sender(job: dict) -> bool:
        delivered.append(dict(job))
        return True

    service = HERv2Learning(
        workspace_dir=root,
        agent_name="agent",
        config_getter=lambda: config,
        invoke_model=invoker,
        audit_log=audit,
        meditation_cost_sender=cost_sender,
    )

    await service.bind_turn(
        notification_context={
            "chat_id": 99,
            "meter_at_start": True,
            "verbose_at_start": False,
            "silent": False,
            "deliver_to_telegram": True,
        }
    ).meditate(
        turn_id="turn-meditation-cost",
        goal="Complete the current request.",
        summary="Finished.",
        evidence_refs=(),
        limitations=(),
        terminal_state=TerminalState.COMPLETED,
    )
    await _wait_for_learning(service)

    assert len(delivered) == 1
    job = delivered[0]
    assert job["request_id"] == "turn-meditation-cost"
    meter = job.get("meter") or {}
    line_items = meter.get("line_items") or []
    assert line_items, "meditation cost line items must be persisted"
    assert line_items[0]["phase"] == "meditation"
    assert line_items[0]["model"] == "configured-learning-model"
    assert line_items[0]["token_source"] == "provider"
    meter_notification = meter.get("notification") or {}
    assert meter_notification["status"] == "sending"
    stored = service.meditation_journal.get(job["job_id"])
    assert stored["meter"]["notification"]["status"] == "sent"
    assert stored["meter"]["notification"]["attempts"] == 1
    assert stored["meter"]["notification"]["delivery_id"].startswith(
        "meditation-cost:"
    )
    assert service.resume_meter_notifications() == 0


@pytest.mark.asyncio
async def test_meditation_cost_delivery_retries_and_then_marks_sent(tmp_path):
    invoker = ScriptedMaintenance(['{"actions":[]}'])
    root = tmp_path / "workspace"
    audit = DurableAuditLog(
        root / "logs" / "her_v2_audit.jsonl",
        root / "backend_state" / "her_v2" / "audit_fallback.jsonl",
    )
    config = HabitMeditationConfig(
        enabled=True,
        max_actions=3,
        meditation_idle_timeout_seconds=0.2,
    )
    calls: list[str] = []

    async def flaky_sender(job: dict) -> bool:
        calls.append(job["job_id"])
        if len(calls) == 1:
            raise RuntimeError("temporary Telegram failure")
        return True

    service = HERv2Learning(
        workspace_dir=root,
        agent_name="agent",
        config_getter=lambda: config,
        invoke_model=invoker,
        audit_log=audit,
        meditation_cost_sender=flaky_sender,
    )

    await service.bind_turn(
        notification_context={
            "chat_id": 99,
            "meter_at_start": True,
            "verbose_at_start": False,
            "silent": False,
            "deliver_to_telegram": True,
        }
    ).meditate(
        turn_id="turn-meditation-retry",
        goal="Complete the current request.",
        summary="Finished.",
        evidence_refs=(),
        limitations=(),
        terminal_state=TerminalState.COMPLETED,
    )
    await _wait_for_learning(service)

    job_id = hashlib.sha256(
        b"her-v2-meditation\0turn-meditation-retry"
    ).hexdigest()[:32]
    stored = service.meditation_journal.get(job_id)
    assert calls == [job_id, job_id]
    assert stored["meter"]["notification"]["status"] == "sent"
    assert stored["meter"]["notification"]["attempts"] == 2


@pytest.mark.asyncio
async def test_meditation_cost_delivery_resumes_after_service_recovery(tmp_path):
    root = tmp_path / "workspace"
    audit = DurableAuditLog(
        root / "logs" / "her_v2_audit.jsonl",
        root / "backend_state" / "her_v2" / "audit_fallback.jsonl",
    )
    config = HabitMeditationConfig(enabled=True, max_actions=3)
    first = HERv2Learning(
        workspace_dir=root,
        agent_name="agent",
        config_getter=lambda: config,
        invoke_model=ScriptedMaintenance(['{"actions":[]}']),
        audit_log=audit,
        meditation_cost_sender=None,
    )

    await first.bind_turn(
        notification_context={
            "chat_id": 99,
            "meter_at_start": True,
            "verbose_at_start": False,
            "silent": False,
            "deliver_to_telegram": True,
        }
    ).meditate(
        turn_id="turn-meditation-recovery",
        goal="Complete the current request.",
        summary="Finished.",
        evidence_refs=(),
        limitations=(),
        terminal_state=TerminalState.COMPLETED,
    )
    await _wait_for_learning(first)

    job_id = hashlib.sha256(
        b"her-v2-meditation\0turn-meditation-recovery"
    ).hexdigest()[:32]
    pending = first.meditation_journal.get(job_id)
    assert pending["meter"]["notification"]["status"] == "pending"

    deliveries: list[str] = []

    async def recovered_sender(job: dict) -> bool:
        deliveries.append(job["meter"]["notification"]["delivery_id"])
        return True

    recovered_service = HERv2Learning(
        workspace_dir=root,
        agent_name="agent",
        config_getter=lambda: config,
        invoke_model=ScriptedMaintenance([]),
        audit_log=audit,
        meditation_cost_sender=recovered_sender,
    )
    recovery = recovered_service.recover()
    await _wait_for_learning(recovered_service)

    assert recovery.resumed_meter_notifications == 1
    assert deliveries == [f"meditation-cost:{job_id}"]
    stored = recovered_service.meditation_journal.get(job_id)
    assert stored["meter"]["notification"]["status"] == "sent"
    assert recovered_service.recover().resumed_meter_notifications == 0
