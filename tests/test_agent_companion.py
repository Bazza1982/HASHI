from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from adapters.stream_events import StreamEvent
from orchestrator.agent_companion import (
    AgentCompanion,
    AgentCompanionSupervisor,
    AgentSnapshot,
    BackgroundJobProcessController,
    CompanionAction,
    CompanionIssue,
    CompanionPolicy,
    HttpJevJudge,
    JevJudgment,
    ManagedProcessLeaseExpired,
    ManagedProcessOwnershipError,
    NullJevJudge,
    TYPESAFE_SYSTEM_ONE_URL,
    build_jev_judge,
    companion_enabled,
)
from tools.builtins import (
    execute_managed_process_start,
    execute_managed_process_status,
    execute_managed_process_stop,
)


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class FakeLane:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    async def interrupt(self, reason: str):
        self.reasons.append(reason)
        return SimpleNamespace(reason=reason)


class FakeJudge:
    def __init__(self, judgment: JevJudgment | None = None) -> None:
        self.judgment = judgment or JevJudgment()
        self.states: list[dict] = []

    async def judge(self, state):
        self.states.append(dict(state))
        return self.judgment


def test_companion_requires_explicit_per_agent_opt_in():
    assert companion_enabled(None) is False
    assert companion_enabled({}) is False
    assert companion_enabled({"agent_companion_enabled": True}) is True
    with pytest.raises(ValueError):
        companion_enabled({"agent_companion_enabled": "true"})


def test_jev_builder_uses_hashi2_fixed_typesafe_endpoint():
    disabled = build_jev_judge({}, {"typesafe_api_key": "secret"})
    assert isinstance(disabled, NullJevJudge)

    missing_key = build_jev_judge({"agent_companion_jev_enabled": True}, {})
    assert isinstance(missing_key, NullJevJudge)

    enabled = build_jev_judge(
        {
            "agent_companion_jev_enabled": True,
            "agent_companion_jev_endpoint": "https://not-used.invalid",
        },
        {"typesafe_api_key": "secret"},
    )
    assert isinstance(enabled, HttpJevJudge)
    assert enabled.endpoint == TYPESAFE_SYSTEM_ONE_URL
    assert enabled.timeout_s == 5.0


@pytest.mark.asyncio
async def test_supervisor_starts_one_companion_per_turn_and_finishes_it():
    clock = FakeClock()
    supervisor = AgentCompanionSupervisor(
        agent_id="arale",
        policy=CompanionPolicy(interval_s=5, progress_grace_s=5),
        clock=clock,
    )

    first = supervisor.start_for_turn(
        run_id="run-1",
        turn_id="turn-1",
        task_summary="bounded task",
    )
    second = supervisor.start_for_turn(
        run_id="run-1",
        turn_id="turn-1",
        task_summary="different duplicate must not create another companion",
    )
    assert first == second == "turn-1"
    assert len(supervisor.snapshots()) == 1

    await supervisor.finish("turn-1")
    assert supervisor.snapshots() == ()
    await supervisor.close()


@pytest.mark.asyncio
async def test_resident_foreground_command_is_interrupted_once_and_state_is_redacted():
    clock = FakeClock(100.0)
    lane = FakeLane()
    events = []
    judge = FakeJudge()
    companion = AgentCompanion(
        AgentSnapshot(
            agent_id="arale",
            run_id="run-1",
            turn_id="turn-1",
            task_summary="run a monitor",
            last_progress_at=clock(),
            can_interrupt=True,
        ),
        control_lane=lane,
        judge=judge,
        event_sink=events.append,
        policy=CompanionPolicy(interval_s=5, progress_grace_s=5),
        clock=clock,
    )
    companion.start()
    companion.observe_stream_event(
        StreamEvent(
            kind="shell_exec",
            tool_name="shell",
            summary="Running command",
            metadata={
                "command": "python monitor.py probe --secret=do-not-send",
                "local_path": "C:\\private\\workspace",
            },
        )
    )

    decision = await companion.tick(force=True)
    duplicate = await companion.tick(force=True)

    assert decision is not None
    assert decision.issue is CompanionIssue.FOREGROUND_RESIDENT_COMMAND
    assert decision.action is CompanionAction.INTERRUPT_TOOL
    assert duplicate is None
    assert len(lane.reasons) == 1
    assert len(events) == 1
    assert "monitor.py" not in judge.states[0]["task_summary"]
    assert "private" not in repr(judge.states[0])
    assert "secret" not in repr(judge.states[0])
    assert "typed managed-process" in events[0].next_step
    await companion.finish()


@pytest.mark.asyncio
async def test_low_confidence_no_progress_warns_without_interrupting():
    clock = FakeClock(10.0)
    lane = FakeLane()
    events = []
    companion = AgentCompanion(
        AgentSnapshot(
            agent_id="arale",
            run_id="run-2",
            turn_id="turn-2",
            task_summary="inspect a bounded command",
            stage="tool",
            current_tool="shell",
            last_progress_at=0.0,
            progress_sequence=1,
            receipt_sequence=1,
            can_interrupt=True,
        ),
        control_lane=lane,
        judge=FakeJudge(
            JevJudgment(
                action=CompanionAction.INTERRUPT_TURN,
                confidence=0.40,
                source="jev",
            )
        ),
        event_sink=events.append,
        policy=CompanionPolicy(interval_s=5, progress_grace_s=5),
        clock=clock,
    )
    companion.start()

    decision = await companion.tick(force=True)

    assert decision is not None
    assert decision.issue is CompanionIssue.REPEATED_NO_PROGRESS
    assert decision.action is CompanionAction.WARN
    assert lane.reasons == []
    assert events[0].action is CompanionAction.WARN
    await companion.finish()


class FakeBackgroundManager:
    def __init__(self, record) -> None:
        self.record = record
        self.cancelled: list[tuple[str, float]] = []

    async def get(self, job_id: str):
        return self.record if job_id == self.record.job_id else None

    async def cancel(self, job_id: str, *, grace_seconds: float):
        self.cancelled.append((job_id, grace_seconds))
        return SimpleNamespace(
            job_id=job_id,
            agent=self.record.agent,
            state="cancelled",
            process=self.record.process,
            origin=self.record.origin,
        )

    async def start_job(self, **kwargs):
        self.started = kwargs
        return self.record


@pytest.mark.asyncio
async def test_managed_process_controller_requires_owner_before_stop():
    record = SimpleNamespace(
        job_id="job-1",
        agent="arale",
        state="running",
        process={"pid": 42, "pgid": 42},
        origin={"managed_process": True, "lease_expires_at": time.time() + 123.0},
    )
    manager = FakeBackgroundManager(record)
    controller = BackgroundJobProcessController(lambda: manager)

    with pytest.raises(ManagedProcessOwnershipError):
        await controller.inspect("zelda", "job-1")
    handle = await controller.inspect("arale", "job-1")
    assert handle is not None
    assert handle.owner == "arale"
    stopped = await controller.stop("arale", "job-1", grace_seconds=1.5)
    assert stopped.state == "cancelled"
    assert manager.cancelled == [("job-1", 1.5)]


@pytest.mark.asyncio
async def test_managed_process_controller_rejects_expired_lease_before_stop():
    record = SimpleNamespace(
        job_id="job-expired",
        agent="arale",
        state="running",
        process={"pid": 42, "pgid": 42},
        origin={"managed_process": True, "managed_owner": "arale", "lease_expires_at": 1.0},
    )
    manager = FakeBackgroundManager(record)
    controller = BackgroundJobProcessController(lambda: manager)

    with pytest.raises(ManagedProcessLeaseExpired):
        await controller.stop("arale", "job-expired")
    assert manager.cancelled == []


@pytest.mark.asyncio
async def test_managed_process_tools_bind_to_current_agent(tmp_path):
    record = SimpleNamespace(
        job_id="job-typed",
        agent="arale",
        state="running",
        process={"pid": 42, "pgid": 42},
        origin={"managed_process": True, "managed_owner": "arale"},
    )
    manager = FakeBackgroundManager(record)
    runtime = SimpleNamespace(orchestrator=SimpleNamespace(background_job_manager=manager))
    context = {"agent_name": "arale", "_runtime": runtime}

    started = await execute_managed_process_start(
        {"argv": ["python", "monitor.py"], "cwd": ".", "label": "probe"},
        access_root=tmp_path,
        workspace_dir=tmp_path,
        audit_context=context,
    )
    assert '"process_id": "job-typed"' in started
    assert manager.started["agent"] == "arale"
    assert manager.started["origin"]["managed_owner"] == "arale"

    status = await execute_managed_process_status(
        {"process_id": "job-typed"}, audit_context=context
    )
    assert '"state": "running"' in status
    stopped = await execute_managed_process_stop(
        {"process_id": "job-typed"}, audit_context=context
    )
    assert '"state": "cancelled"' in stopped


def test_compact_snapshot_contains_no_prompt_or_local_path():
    snapshot = AgentSnapshot(
        agent_id="zelda",
        run_id="run-3",
        turn_id="turn-3",
        task_summary="summarised task",
        last_progress_at=5.0,
    )
    compact = snapshot.compact_state(now=10.0)
    assert "prompt" not in compact
    assert "local_path" not in compact
    assert compact["progress_age_seconds"] == 5.0


def test_custom_resident_markers_are_used_by_companion():
    companion = AgentCompanion(
        AgentSnapshot(
            agent_id="zelda",
            run_id="run-4",
            turn_id="turn-4",
            last_progress_at=0.0,
        ),
        policy=CompanionPolicy(resident_markers=("custom-daemon",)),
    )
    assert companion._looks_resident("start custom-daemon now")
    assert not companion._looks_resident("start monitor.py now")
