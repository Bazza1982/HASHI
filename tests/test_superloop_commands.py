from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from orchestrator.runtime_superloop import handle_superloop_command
from orchestrator.superloop_scheduler import advance_superloops_once
from orchestrator.superloop_store import SuperloopStore


class _FakeRuntime:
    def __init__(self, root: Path):
        self.name = "zelda"
        self.global_config = SimpleNamespace(project_root=root)
        self.messages: list[str] = []

    async def _reply_text(self, _update, text: str, **_kwargs):
        self.messages.append(text)


class _FakeUpdate:
    def __init__(self, text: str):
        self.message = SimpleNamespace(text=text)


@pytest.mark.asyncio
async def test_superloop_record_start_try_finish_status(tmp_path: Path) -> None:
    runtime = _FakeRuntime(tmp_path)

    await handle_superloop_command(runtime, _FakeUpdate("/superloop record start test loop goal"), "record start test loop goal")
    assert any("recording started" in text.lower() for text in runtime.messages)
    rec_line = next(text for text in runtime.messages if "recording_id:" in text)
    recording_id = rec_line.split("`")[1]

    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop record try {recording_id} first step"), f"record try {recording_id} first step")
    assert any("Recorded trial step" in text for text in runtime.messages)

    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop record finish {recording_id}"), f"record finish {recording_id}")
    compiled_text = next(text for text in runtime.messages if "Superloop compiled" in text)
    loop_id = compiled_text.split("`")[3]
    assert loop_id.startswith("sl-")

    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop record status {recording_id}"), f"record status {recording_id}")
    assert any("recording status" in text.lower() for text in runtime.messages)

    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop status {loop_id}"), f"status {loop_id}")
    assert any("Superloop status" in text for text in runtime.messages)

    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop resume {loop_id}"), f"resume {loop_id}")
    assert any("Resumed" in text for text in runtime.messages)

    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop next {loop_id}"), f"next {loop_id}")
    assert any("Next action evaluated" in text for text in runtime.messages)

    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop task add {loop_id} review notes"), f"task add {loop_id} review notes")
    assert any("task added" in text for text in runtime.messages)

    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop issue add {loop_id} reviewer missing"), f"issue add {loop_id} reviewer missing")
    assert any("issue opened" in text for text in runtime.messages)

    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop wait add {loop_id} await_hchat_reply"), f"wait add {loop_id} await_hchat_reply")
    assert any("wait added" in text for text in runtime.messages)

    state = SuperloopStore(tmp_path / "superloops").load_loop_state(loop_id)
    assert state["stats"]["task_total"] == 2
    assert state["stats"]["issue_open"] == 1
    assert state["stats"]["wait_open"] == 1

    await handle_superloop_command(
        runtime,
        _FakeUpdate(f"/superloop wait add {loop_id} sleep_until 2099-01-01T00:00:00+00:00"),
        f"wait add {loop_id} sleep_until 2099-01-01T00:00:00+00:00",
    )
    assert sum(1 for text in runtime.messages if "wait added" in text) >= 2


@pytest.mark.asyncio
async def test_superloop_scheduler_advances_after_past_sleep_until(tmp_path: Path) -> None:
    runtime = _FakeRuntime(tmp_path)
    await handle_superloop_command(runtime, _FakeUpdate("/superloop record start scheduler e2e"), "record start scheduler e2e")
    rec_line = next(text for text in runtime.messages if "recording_id:" in text)
    recording_id = rec_line.split("`")[1]
    await handle_superloop_command(
        runtime,
        _FakeUpdate(f"/superloop record try {recording_id} scheduler first step"),
        f"record try {recording_id} scheduler first step",
    )
    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop record finish {recording_id}"), f"record finish {recording_id}")
    compiled_text = next(text for text in runtime.messages if "Superloop compiled" in text)
    loop_id = compiled_text.split("`")[3]
    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop resume {loop_id}"), f"resume {loop_id}")

    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop task add {loop_id} first actionable"), f"task add {loop_id} first actionable")
    await handle_superloop_command(
        runtime,
        _FakeUpdate(f"/superloop wait add {loop_id} sleep_until 2000-01-01T00:00:00+00:00"),
        f"wait add {loop_id} sleep_until 2000-01-01T00:00:00+00:00",
    )

    stats = advance_superloops_once(tmp_path / "superloops")
    assert stats["loops_checked"] >= 1
    assert stats["waits_satisfied"] >= 1

    state = SuperloopStore(tmp_path / "superloops").load_loop_state(loop_id)
    assert state.get("current_step")
    assert state.get("next_action", {}).get("kind") == "run_task"


@pytest.mark.asyncio
async def test_superloop_quickstart_and_wizard(tmp_path: Path) -> None:
    runtime = _FakeRuntime(tmp_path)

    await handle_superloop_command(runtime, _FakeUpdate("/superloop quickstart demo goal"), "quickstart demo goal")
    assert any("Quickstart" in text for text in runtime.messages)
    quick_text = next(text for text in runtime.messages if "loop_id:" in text and "Quickstart" in text)
    loop_id = quick_text.split("`")[3]
    state = SuperloopStore(tmp_path / "superloops").load_loop_state(loop_id)
    assert state.get("status") == "running"
    events_path = tmp_path / "superloops" / "loops" / loop_id / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    created_events = [event for event in events if event.get("kind") == "loop.created"]
    assert len(created_events) == 1
    assert created_events[0]["data"]["recording_id"].startswith("slrec-")

    await handle_superloop_command(runtime, _FakeUpdate("/superloop wizard wizard goal"), "wizard wizard goal")
    assert any("Wizard" in text for text in runtime.messages)


@pytest.mark.asyncio
async def test_superloop_help_is_visual(tmp_path: Path) -> None:
    runtime = _FakeRuntime(tmp_path)
    await handle_superloop_command(runtime, _FakeUpdate("/superloop"), "")
    help_text = runtime.messages[-1]
    assert "快速开始" in help_text
    assert "/superloop quickstart <goal>" in help_text
