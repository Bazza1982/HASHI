from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

from orchestrator.agent_runtime import BridgeAgentRuntime
from orchestrator import runtime_jobs
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime


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
        runtime._job_transfer_selections = {
            f"old{idx}": {"kind": "cron", "task_id": "old", "target_agent": "agent"}
            for idx in range(256)
        }

        callback_data = runtime._job_transfer_callback("cron", "new-task", "zhaojun")

        assert callback_data == "skilljob:cron:xferkey:jtx1:go"
        assert list(runtime._job_transfer_selections) == ["jtx1"]


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
