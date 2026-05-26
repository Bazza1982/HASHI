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

    await handle_superloop_command(runtime, _FakeUpdate(f"/superloop validate {loop_id}"), f"validate {loop_id}")
    assert any("Superloop validation" in text for text in runtime.messages)

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
    assert all(event.get("actor", {}).get("agent") for event in events)

    await handle_superloop_command(runtime, _FakeUpdate("/superloop wizard wizard goal"), "wizard wizard goal")
    assert any("Wizard" in text for text in runtime.messages)


@pytest.mark.asyncio
async def test_superloop_help_is_visual(tmp_path: Path) -> None:
    runtime = _FakeRuntime(tmp_path)
    await handle_superloop_command(runtime, _FakeUpdate("/superloop"), "")
    help_text = runtime.messages[-1]
    assert "快速开始" in help_text
    assert "/superloop quickstart <goal>" in help_text
    assert "/superloop list" in help_text
    assert "/superloop validate <loop_id>" in help_text
    assert "/superloop closeout <loop_id>" in help_text


@pytest.mark.asyncio
async def test_superloop_list_shows_templates(tmp_path: Path) -> None:
    runtime = _FakeRuntime(tmp_path)
    templates_root = tmp_path / "superloops" / "templates"
    auto_debug = templates_root / "auto_debug"
    auto_debug.mkdir(parents=True)
    (auto_debug / "README.md").write_text(
        "# Auto Debug Superloop Template\n\n## Purpose\n\nRun a bug investigation and repair loop.\n",
        encoding="utf-8",
    )
    (auto_debug / "taskboard.template.json").write_text("[]", encoding="utf-8")

    remote_install = templates_root / "remote_install"
    remote_install.mkdir(parents=True)
    (remote_install / "README.md").write_text(
        "# Remote Install Superloop Template\n\n## Purpose\n\nInstall a prepared HASHI package onto a remote machine.\n",
        encoding="utf-8",
    )
    (remote_install / "taskboard.template.json").write_text("[]", encoding="utf-8")
    (remote_install / "roles.template.json").write_text("[]", encoding="utf-8")

    await handle_superloop_command(runtime, _FakeUpdate("/superloop list"), "list")
    list_text = runtime.messages[-1]
    assert "模板列表" in list_text
    assert "Auto Debug Superloop Template" in list_text
    assert "Remote Install Superloop Template" in list_text
    assert "slug: `auto_debug`" in list_text
    assert "包含: `README · taskboard`" in list_text


@pytest.mark.asyncio
async def test_superloop_closeout_blocks_missing_hchat_receipt(tmp_path: Path) -> None:
    runtime = _FakeRuntime(tmp_path)
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-closeout",
        loop_state={
            "loop_id": "sl-test-closeout",
            "status": "running",
            "taskboard_path": "superloops/loops/sl-test-closeout/taskboard.json",
            "issues_path": "superloops/loops/sl-test-closeout/issues.json",
            "waits_path": "superloops/loops/sl-test-closeout/waits.json",
        },
        taskboard=[
            {
                "task_id": "task-001",
                "title": "Ask Nana",
                "status": "completed",
                "owner_agent": "nana",
                "owner_instance": "HASHI1",
                "depends_on": [],
                "execution_mode": "hchat_agent",
            }
        ],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )

    await handle_superloop_command(runtime, _FakeUpdate("/superloop closeout sl-test-closeout"), "closeout sl-test-closeout")

    assert "blocking: `True`" in runtime.messages[-1]
    assert "hchat_task_missing_receipt" in runtime.messages[-1]
    assert store.load_loop_state("sl-test-closeout")["status"] == "running"


@pytest.mark.asyncio
async def test_superloop_closeout_accepts_validated_loop(tmp_path: Path) -> None:
    runtime = _FakeRuntime(tmp_path)
    store = SuperloopStore(tmp_path / "superloops")
    transcript = tmp_path / "workspaces" / "nana" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        '{"role":"assistant","text":"done sl-test-closeout-ok task-001 receipt-nana artifact nana_report.md"}\n',
        encoding="utf-8",
    )
    artifact = tmp_path / "nana_report.md"
    artifact.write_text("# Nana report\n", encoding="utf-8")
    store.create_compiled_loop(
        loop_id="sl-test-closeout-ok",
        loop_state={
            "loop_id": "sl-test-closeout-ok",
            "status": "running",
            "taskboard_path": "superloops/loops/sl-test-closeout-ok/taskboard.json",
            "issues_path": "superloops/loops/sl-test-closeout-ok/issues.json",
            "waits_path": "superloops/loops/sl-test-closeout-ok/waits.json",
        },
        taskboard=[
            {
                "task_id": "task-001",
                "title": "Ask Nana",
                "status": "completed",
                "owner_agent": "nana",
                "owner_instance": "HASHI1",
                "depends_on": [],
                "execution_mode": "hchat_agent",
                "dispatch_refs": ["dispatch_nana.md"],
                "receipt_refs": ["receipt-nana"],
                "artifact_refs": ["nana_report.md"],
                "receipt_sources": [
                    {
                        "agent": "nana",
                        "transcript_path": "workspaces/nana/transcript.jsonl",
                        "line_start": 1,
                        "line_end": 1,
                        "artifact_path": "nana_report.md",
                    }
                ],
            }
        ],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )

    await handle_superloop_command(runtime, _FakeUpdate("/superloop closeout sl-test-closeout-ok"), "closeout sl-test-closeout-ok")

    assert "closeout accepted" in runtime.messages[-1]
    assert store.load_loop_state("sl-test-closeout-ok")["status"] == "completed"
    events_path = tmp_path / "superloops" / "loops" / "sl-test-closeout-ok" / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    completed_events = [event for event in events if event.get("kind") == "loop.completed"]
    assert completed_events
    assert completed_events[-1]["actor"]["agent"] == "zelda"
    assert completed_events[-1]["actor"]["source"] == "superloop_command"
