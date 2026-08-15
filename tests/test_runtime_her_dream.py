from __future__ import annotations

import asyncio
import inspect
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from adapters.her_dream import HERDreamJournal
from adapters.her_habits import HERHabitStore
from orchestrator import runtime_her_dream
from orchestrator import scheduler as scheduler_module
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.scheduler import TaskScheduler
from orchestrator.skill_manager import SkillManager


class FakeDreamAdapter:
    def __init__(self, workspace: Path):
        self.config = SimpleNamespace(engine="her", workspace_dir=workspace)
        self._store = HERHabitStore(workspace)
        self._journal = HERDreamJournal(workspace)
        self._habit_execution_lock = asyncio.Lock()
        self._habit_dream_execution_lock = asyncio.Lock()
        self._habit_dream_run_lock = asyncio.Lock()
        self.calls: list[dict[str, Any]] = []
        self.responses: list[Any] = []

    def _her_habit_store(self) -> HERHabitStore:
        return self._store

    def _her_dream_journal(self) -> HERDreamJournal:
        return self._journal

    async def run_habit_dream_model(
        self,
        prompt: str,
        *,
        request_id: str,
        timeout_seconds: float = 600.0,
    ) -> Any:
        self.calls.append(
            {
                "prompt": prompt,
                "request_id": request_id,
                "timeout_seconds": timeout_seconds,
                "write_lock_held": self._habit_execution_lock.locked(),
            }
        )
        if self.responses:
            response = self.responses.pop(0)
            if callable(response):
                response = response(prompt, request_id)
            if inspect.isawaitable(response):
                response = await response
            if isinstance(response, Exception):
                raise response
            return SimpleNamespace(text=str(response))
        if "PERSONA WRAPPER" in prompt:
            return SimpleNamespace(text='{"intro":"A gentle catalogue check is complete.","closing":"Everything remains recoverable."}')
        return SimpleNamespace(text='{"groups":[]}')


class FakeDreamRuntime:
    def __init__(
        self,
        workspace: Path,
        *,
        engine: str = "her",
        skill_manager: SkillManager | None = None,
    ):
        self.name = "zelda"
        self.workspace_dir = workspace
        self.transcript_log_path = workspace / "transcript.jsonl"
        self.logger = logging.getLogger(f"test.dream.{engine}")
        self.error_logger = self.logger
        self.config = SimpleNamespace(active_backend=engine)
        if engine == "her":
            adapter: Any = FakeDreamAdapter(workspace)
        else:
            adapter = SimpleNamespace(config=SimpleNamespace(engine=engine))
        self.backend_manager = SimpleNamespace(current_backend=adapter)
        self.skill_manager = skill_manager
        self.sys_prompt_manager = SimpleNamespace(get_active_texts=list)
        self.replies: list[dict[str, Any]] = []
        self.long_messages: list[dict[str, Any]] = []

    def _is_authorized_user(self, user_id: int | None) -> bool:
        return user_id == 1

    def _is_command_allowed(self, command: str) -> bool:
        return command == "dream"

    def _primary_chat_id(self) -> int:
        return 99

    async def _reply_text(self, update: Any, text: str, **kwargs: Any) -> None:
        self.replies.append({"text": text, **kwargs})

    async def send_long_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        self.long_messages.append({"chat_id": chat_id, "text": text, **kwargs})


def _seed_habit(store: HERHabitStore, title: str = "Verify current state") -> str:
    [outcome] = store.apply_actions(
        [
            {
                "operation": "create",
                "title": title,
                "metadata": "Use when a workflow may have changed while it was running.",
                "body": "Inspect the current state before deciding what to do next.",
            }
        ],
        max_actions=1,
    )
    return outcome.split(":", 1)[1]


def _command_update() -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=99),
    )


def test_schedule_presets_validate_against_active_scheduler_capability(monkeypatch):
    assert runtime_her_dream.compile_schedule(["daily", "02:30"]) == "30 2 * * *"

    monkeypatch.setattr(scheduler_module, "HAS_CRONITER", False)
    with pytest.raises(ValueError, match="lacks croniter"):
        runtime_her_dream.compile_schedule(["weekly", "sun", "02:30"])
    with pytest.raises(ValueError, match="lacks croniter"):
        runtime_her_dream.compile_schedule(
            ["cron", "*/15", "*", "*", "*", "*"]
        )


def test_enabled_legacy_schedule_migrates_only_for_her(tmp_path):
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(
        json.dumps(
            {
                "crons": [
                    {
                        "id": "dream-zelda-nightly",
                        "agent": "zelda",
                        "enabled": True,
                        "time": "01:30",
                        "action": "skill:dream",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    manager = SkillManager(project_root=tmp_path, tasks_path=tasks_path)
    runtime = FakeDreamRuntime(tmp_path / "workspace", skill_manager=manager)

    result = runtime_her_dream.migrate_legacy_schedule(runtime)

    assert result["created"] is True
    tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    legacy = next(job for job in tasks["crons"] if job["id"] == "dream-zelda-nightly")
    native = next(job for job in tasks["crons"] if job["id"] == "her-dream-zelda")
    assert legacy["enabled"] is False
    assert native["enabled"] is True
    assert native["schedule"] == "30 1 * * *"
    assert native["action"] == "her:dream"
    assert runtime_her_dream.migrate_legacy_schedule(runtime)["changed"] is False


def test_enabled_legacy_schedule_is_disabled_without_reading_non_her_habits(
    tmp_path,
    monkeypatch,
):
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(
        json.dumps(
            {
                "crons": [
                    {
                        "id": "dream-zelda-nightly",
                        "agent": "zelda",
                        "enabled": True,
                        "schedule": "30 1 * * *",
                        "action": "skill:dream",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    manager = SkillManager(project_root=tmp_path, tasks_path=tasks_path)
    runtime = FakeDreamRuntime(
        tmp_path / "workspace",
        engine="codex-cli",
        skill_manager=manager,
    )

    monkeypatch.setattr(
        runtime_her_dream,
        "_store",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("non-HER migration must not inspect Habits")
        ),
    )
    result = runtime_her_dream.migrate_legacy_schedule(runtime)

    tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    assert result["created"] is False
    assert tasks["crons"][0]["enabled"] is False
    assert not any(job["id"] == "her-dream-zelda" for job in tasks["crons"])


@pytest.mark.asyncio
async def test_non_her_manual_is_blocked_but_scheduled_run_is_visible_skip(
    tmp_path,
    monkeypatch,
):
    runtime = FakeDreamRuntime(tmp_path, engine="codex-cli")
    monkeypatch.setattr(
        runtime_her_dream,
        "_store",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("non-HER Dream must not inspect Habits")
        ),
    )

    manual_ok, manual_report, _ = await runtime_her_dream.execute_dream(
        runtime,
        origin="manual",
    )
    scheduled_ok, scheduled_report, _ = await runtime_her_dream.execute_dream(
        runtime,
        origin="scheduled:her-dream-zelda",
    )

    assert manual_ok is False
    assert "only while this agent uses HER" in manual_report
    assert scheduled_ok is True
    assert "schedule remains enabled" in scheduled_report
    assert not (tmp_path / "habits").exists()


@pytest.mark.asyncio
async def test_non_her_status_does_not_read_habit_bearing_dream_manifests(
    tmp_path,
    monkeypatch,
):
    runtime = FakeDreamRuntime(tmp_path, engine="codex-cli")

    def forbidden_manifest_read(*_args: Any, **_kwargs: Any):
        raise AssertionError("non-HER status must not inspect Dream manifests")

    monkeypatch.setattr(HERDreamJournal, "latest_run", forbidden_manifest_read)
    monkeypatch.setattr(
        runtime_her_dream.her_dream,
        "latest_undoable_run",
        forbidden_manifest_read,
    )

    await runtime_her_dream.cmd_dream(
        runtime,
        _command_update(),
        SimpleNamespace(args=["status"]),
    )

    assert "No dormant Habit files were read" in runtime.replies[-1]["text"]
    assert not (tmp_path / "habits").exists()


@pytest.mark.asyncio
async def test_dream_no_change_uses_persona_fallback_and_advances_cursor(tmp_path):
    runtime = FakeDreamRuntime(tmp_path)
    adapter: FakeDreamAdapter = runtime.backend_manager.current_backend
    _seed_habit(adapter._store)
    (tmp_path / "AGENT.md").write_text(
        "Use a warm persona. api_key=persona-secret-value-123456",
        encoding="utf-8",
    )
    runtime.transcript_log_path.write_text(
        json.dumps(
            {
                "ts": "2026-08-15T01:00:00Z",
                "role": "user",
                "source": "text",
                "text": "Keep this as an explicit standing preference.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    adapter.responses = ['{"groups":[]}', "not-json"]

    ok, report, manifest = await runtime_her_dream.execute_dream(
        runtime,
        origin="manual",
    )

    assert ok is True
    assert manifest is not None and manifest["status"] == "no_change"
    assert report.startswith("🌙 Dream completed")
    assert "No eligible Habit changes were found" in report
    assert all(call["write_lock_held"] is False for call in adapter.calls)
    assert "persona-secret-value-123456" not in adapter.calls[-1]["prompt"]
    assert "[REDACTED_SECRET]" in adapter.calls[-1]["prompt"]
    cursor = adapter._journal.read_cursor()
    assert cursor["offset"] == runtime.transcript_log_path.stat().st_size
    audit = adapter._journal.audit_path.read_text(encoding="utf-8")
    assert "dream_persona_fallback" in audit


@pytest.mark.asyncio
async def test_dream_retries_once_after_concurrent_habit_change(tmp_path):
    runtime = FakeDreamRuntime(tmp_path)
    adapter: FakeDreamAdapter = runtime.backend_manager.current_backend
    habit_id = _seed_habit(adapter._store)

    def first_response(_prompt: str, _request_id: str) -> str:
        adapter._store.apply_actions(
            [
                {
                    "operation": "update",
                    "habit_id": habit_id,
                    "body": "A concurrent Meditation committed a newer canonical behaviour.",
                }
            ],
            max_actions=1,
        )
        return json.dumps(
            {
                "groups": [
                    {
                        "operation": "archive",
                        "habit_id": habit_id,
                        "reason": "The previous wording appeared obsolete.",
                    }
                ]
            }
        )

    adapter.responses = [
        first_response,
        '{"groups":[]}',
        '{"intro":"The fresh catalogue is settled.","closing":"No unsafe overwrite occurred."}',
    ]

    ok, report, manifest = await runtime_her_dream.execute_dream(
        runtime,
        origin="manual",
    )

    assert ok is True
    assert manifest is not None and manifest["status"] == "no_change"
    assert adapter._store.get(habit_id) is not None
    assert "No eligible Habit changes" in report
    run = adapter._journal.get_run(str(manifest["run_id"]))
    assert len(run["attempts"]) == 2
    assert "dream_stale_retry" in adapter._journal.audit_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_cancelled_dream_is_tracked_and_journalled(tmp_path):
    runtime = FakeDreamRuntime(tmp_path)
    adapter: FakeDreamAdapter = runtime.backend_manager.current_backend
    adapter._habit_dream_tasks = set()
    _seed_habit(adapter._store)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_response(_prompt: str, _request_id: str) -> str:
        started.set()
        await release.wait()
        return '{"groups":[]}'

    adapter.responses = [blocking_response]
    task = asyncio.create_task(
        runtime_her_dream.execute_dream(runtime, origin="manual")
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    assert task in adapter._habit_dream_tasks

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task not in adapter._habit_dream_tasks
    latest = adapter._journal.latest_run()
    assert latest is not None and latest["status"] == "cancelled"


@pytest.mark.asyncio
async def test_scheduled_invocation_always_delivers_result(tmp_path, monkeypatch):
    runtime = FakeDreamRuntime(tmp_path)
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        runtime_her_dream,
        "migrate_legacy_schedule",
        lambda _runtime: {"changed": False},
    )

    async def fake_execute(_runtime: Any, *, origin: str):
        captured["origin"] = origin
        return False, "🌙 deterministic failure report", None

    monkeypatch.setattr(runtime_her_dream, "execute_dream", fake_execute)

    ok, report = await FlexibleAgentRuntime.invoke_her_dream(
        runtime,
        task_id="her-dream-zelda",
        scheduled_for="2026-08-15T02:30+10:00",
    )

    assert ok is False
    assert report == "🌙 deterministic failure report"
    assert captured["origin"].endswith("2026-08-15T02:30+10:00")
    assert runtime.long_messages[0]["text"] == report
    assert runtime.long_messages[0]["purpose"] == "her-dream-scheduled"


@pytest.mark.asyncio
async def test_scheduler_routes_legacy_dream_action_to_native_runtime(tmp_path):
    calls: list[dict[str, Any]] = []

    class Runtime:
        name = "zelda"
        startup_success = True

        async def invoke_her_dream(self, **kwargs: Any):
            calls.append(kwargs)
            return True, "done"

    runtime = Runtime()
    scheduler = TaskScheduler(
        tasks_path=tmp_path / "tasks.json",
        state_path=tmp_path / "scheduler-state.json",
        runtimes=[runtime],
        authorized_id=1,
    )
    job = {
        "id": "dream-zelda-nightly",
        "agent": "zelda",
        "enabled": True,
        "schedule": "30 1 * * *",
        "action": "skill:dream",
    }

    ok = await scheduler._fire_cron_job(
        job,
        runtime_map={"zelda": runtime},
        tasks={"crons": [job]},
        now_dt=scheduler_module.datetime.now(),
    )

    assert ok is True
    assert calls == [{"task_id": "dream-zelda-nightly", "scheduled_for": None}]


@pytest.mark.asyncio
async def test_skill_dream_is_a_native_command_compatibility_route(
    monkeypatch,
):
    routed: list[list[str]] = []

    class Runtime:
        skill_manager = None

        def _is_authorized_user(self, user_id: int | None) -> bool:
            return user_id == 1

    async def fake_cmd_dream(
        _runtime: Any,
        _update: Any,
        _context: Any,
        *,
        args_override: list[str] | None = None,
    ) -> None:
        routed.append(list(args_override or []))

    monkeypatch.setattr(runtime_her_dream, "cmd_dream", fake_cmd_dream)

    await FlexibleAgentRuntime.cmd_skill(
        Runtime(),
        _command_update(),
        SimpleNamespace(args=["dream", "schedule", "daily", "03:15"]),
    )

    assert routed == [["schedule", "daily", "03:15"]]
