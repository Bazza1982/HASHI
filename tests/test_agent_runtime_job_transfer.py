from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

from orchestrator import runtime_jobs
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.legacy.bridge_agent_runtime import BridgeAgentRuntime


class _FakeSkillManager:
    def __init__(self):
        self.jobs = {
            "cron": {
                "arale-daily-security-scan": {
                    "id": "arale-daily-security-scan",
                    "agent": "arale",
                    "enabled": True,
                    "note": "daily security scan",
                    "prompt": "scan",
                }
            }
        }
        self.transfers = []

    def get_job(self, kind: str, task_id: str):
        return self.jobs.get(kind, {}).get(task_id)

    def transfer_job(self, kind: str, task_id: str, target_agent: str):
        self.transfers.append((kind, task_id, target_agent))
        return True, f"Transferred to {target_agent} (disabled, review before enabling).", {
            "id": "zhaojun-12345678",
            "agent": target_agent,
            "enabled": False,
        }


class _FakeQuery:
    def __init__(self, data: str):
        self.data = data
        self.from_user = SimpleNamespace(id=1)
        self.message = SimpleNamespace(chat_id=1)
        self.answers = []
        self.edits = []

    async def answer(self, text=None, **kwargs):
        self.answers.append({"text": text, **kwargs})

    async def edit_message_text(self, text, **kwargs):
        self.edits.append({"text": text, **kwargs})


def _runtime():
    runtime = object.__new__(BridgeAgentRuntime)
    runtime.name = "arale"
    runtime.global_config = SimpleNamespace(
        authorized_id=1,
        project_root=Path("/tmp/hashi-test"),
    )
    runtime.skill_manager = _FakeSkillManager()
    runtime.orchestrator = SimpleNamespace(
        runtimes=[
            SimpleNamespace(name="arale"),
            SimpleNamespace(name="zhaojun"),
        ]
    )
    return runtime


@pytest.mark.asyncio
async def test_fixed_agent_jobs_transfer_button_shows_target_selector():
    runtime = _runtime()
    query = _FakeQuery("skilljob:cron:transfer:arale-daily-security-scan:select")

    await BridgeAgentRuntime.callback_skill(runtime, SimpleNamespace(callback_query=query), SimpleNamespace())

    assert query.answers[-1]["text"] is None
    assert "Transfer job" in query.edits[-1]["text"]
    markup = query.edits[-1]["reply_markup"]
    keyboard = markup.inline_keyboard
    zhaojun_buttons = [button for row in keyboard for button in row if button.text == "zhaojun"]
    assert zhaojun_buttons
    assert zhaojun_buttons[0].callback_data.startswith("skilljob:cron:xferkey:")
    assert len(zhaojun_buttons[0].callback_data) <= 64


@pytest.mark.asyncio
async def test_fixed_agent_jobs_transfer_target_moves_job():
    runtime = _runtime()
    callback_data = runtime._job_transfer_callback("cron", "arale-daily-security-scan", "zhaojun")
    query = _FakeQuery(callback_data)

    await BridgeAgentRuntime.callback_skill(runtime, SimpleNamespace(callback_query=query), SimpleNamespace())

    assert runtime.skill_manager.transfers == [("cron", "arale-daily-security-scan", "zhaojun")]
    assert query.answers[-1]["text"].startswith("Transferred to zhaojun")
    assert "Job transferred to <b>zhaojun</b>" in query.edits[-1]["text"]


def test_flexible_job_transfer_keyboard_uses_short_callbacks_for_long_targets():
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "arale"
    runtime.global_config = SimpleNamespace(project_root=Path("/tmp/hashi-test"))
    runtime.orchestrator = SimpleNamespace(
        runtimes=[
            SimpleNamespace(name="arale"),
            SimpleNamespace(name="zhaojun"),
            SimpleNamespace(name="lin_yueru"),
        ]
    )

    markup = FlexibleAgentRuntime._build_job_transfer_keyboard(
        runtime,
        "cron",
        "arale-daily-security-scan",
    )

    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert callbacks
    assert all(len(callback_data) <= 64 for callback_data in callbacks)
    assert any(callback_data.startswith("skilljob:cron:xferkey:") for callback_data in callbacks)


def test_job_transfer_token_store_is_bounded_for_fixed_and_flexible_runtimes():
    for runtime_cls in (BridgeAgentRuntime, FlexibleAgentRuntime):
        runtime = object.__new__(runtime_cls)
        runtime._ui_callback_tokens = {
            "skilljob_transfer": {
                f"old{idx}": {
                    "payload": {"kind": "cron", "task_id": "old", "target_agent": "agent"},
                    "created_at": float(idx),
                    "expires_at": 9999999999.0,
                }
                for idx in range(256)
            }
        }
        runtime._job_transfer_selections = {}

        callback_data = runtime._job_transfer_callback("cron", "new-task", "zhaojun")

        assert callback_data.startswith("skilljob:cron:xferkey:jtx")
        assert len(callback_data) <= runtime_jobs.CALLBACK_DATA_LIMIT
        token = callback_data.split(":")[3]
        assert list(runtime._job_transfer_selections) == [token]
        assert len(runtime._ui_callback_tokens["skilljob_transfer"]) == 256


def test_flexible_job_transfer_keyboard_logs_remote_instance_errors(tmp_path):
    root = tmp_path / "hashi"
    root.mkdir()
    (root / "instances.json").write_text("{not json", encoding="utf-8")
    warnings = []
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "arale"
    runtime.global_config = SimpleNamespace(project_root=root)
    runtime.orchestrator = SimpleNamespace(runtimes=[])
    runtime.logger = SimpleNamespace(warning=lambda message, *args: warnings.append(message % args))

    markup = FlexibleAgentRuntime._build_job_transfer_keyboard(runtime, "cron", "task")

    assert warnings
    assert "Failed to build remote agent transfer buttons" in warnings[0]
    assert markup.inline_keyboard[-1][0].text == "✖ Cancel"


@pytest.mark.asyncio
async def test_runtime_jobs_handle_flexible_xferkey_transfer():
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.skill_manager = _FakeSkillManager()
    runtime._job_transfer_selections = {
        "jtx1": {
            "kind": "cron",
            "task_id": "arale-daily-security-scan",
            "target_agent": "zhaojun",
            "instance_id": None,
            "remote": False,
        }
    }
    query = _FakeQuery("skilljob:cron:xferkey:jtx1:go")

    handled = await runtime_jobs.handle_skill_job_callback(runtime, query, query.data)

    assert handled is True
    assert runtime.skill_manager.transfers == [("cron", "arale-daily-security-scan", "zhaojun")]
    assert query.answers[-1]["text"].startswith("Transferred to zhaojun")
    assert "Job transferred to <b>zhaojun</b>" in query.edits[-1]["text"]


def test_jobs_panel_uses_short_tokenized_callbacks_for_long_job_ids(tmp_path):
    tasks_path = tmp_path / "tasks.json"
    long_id = "lin_yueru-loop-hashi-remote-watchdog-7d"
    tasks_path.write_text(
        json.dumps(
            {
                "heartbeats": [
                    {
                        "id": long_id,
                        "agent": "lin_yueru",
                        "enabled": True,
                        "interval_seconds": 7200,
                        "note": "watchdog",
                    }
                ],
                "crons": [],
            }
        ),
        encoding="utf-8",
    )
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "lin_yueru"
    runtime.global_config = SimpleNamespace(project_root=tmp_path)
    runtime.skill_manager = SimpleNamespace(tasks_path=tasks_path)

    text, markup = runtime_jobs._build_jobs_with_buttons(runtime, runtime.name, runtime.skill_manager, filter_agent=runtime.name)

    assert long_id in text
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data != "noop"
    ]
    assert callbacks
    assert all(len(callback_data) <= runtime_jobs.CALLBACK_DATA_LIMIT for callback_data in callbacks)
    assert any(callback_data.startswith("skilljob:heartbeat:key:") for callback_data in callbacks)


@pytest.mark.asyncio
async def test_runtime_jobs_handle_tokenized_toggle_callback():
    class SkillManager(_FakeSkillManager):
        def __init__(self):
            super().__init__()
            self.enabled_changes = []

        def set_job_enabled(self, kind: str, task_id: str, enabled: bool):
            self.enabled_changes.append((kind, task_id, enabled))
            return True, f"set {task_id}"

    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.skill_manager = SkillManager()
    render_calls = []

    async def _render(query, kind):
        render_calls.append((query, kind))

    runtime._render_skill_jobs = _render
    token = runtime_jobs.mint_callback_token(
        runtime,
        "skilljob_action",
        {"kind": "cron", "task_id": "arale-daily-security-scan", "action": "toggle", "value": "off"},
        prefix="j",
    )
    query = _FakeQuery(f"skilljob:cron:key:{token}:toggle")

    handled = await runtime_jobs.handle_skill_job_callback(runtime, query, query.data)

    assert handled is True
    assert runtime.skill_manager.enabled_changes == [("cron", "arale-daily-security-scan", False)]
    assert render_calls == [(query, "cron")]


@pytest.mark.asyncio
async def test_runtime_jobs_expired_token_shows_alert():
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.skill_manager = _FakeSkillManager()
    query = _FakeQuery("skilljob:cron:key:jdeadbe:run")

    handled = await runtime_jobs.handle_skill_job_callback(runtime, query, query.data)

    assert handled is True
    assert query.answers[-1]["text"] == "This jobs action expired. Open /jobs again."
    assert query.answers[-1]["show_alert"] is True
