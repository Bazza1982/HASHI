from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import ui_language
from orchestrator.config import GlobalConfig
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.pcm import render_pcm_document
from orchestrator.phone_manager import PhoneManager


def _runtime(tmp_path):
    workspace = tmp_path / "moon"
    workspace.mkdir()
    (workspace / "agent.md").write_text(
        render_pcm_document(
            persona="You are Moon and speak respectfully.",
            system="Private runtime rules.",
        ),
        encoding="utf-8",
    )
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.name = "moon"
    runtime.config = SimpleNamespace(extra={"display_name": "Moon"})
    runtime.phone_manager = PhoneManager(workspace)
    runtime.global_config = SimpleNamespace(live_voice_v1=True)
    runtime.secrets = {}
    runtime._is_authorized_user = lambda _user_id: True
    runtime._reply_text = AsyncMock()
    return runtime


def test_phone_menu_reports_missing_key_without_internal_revision(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    runtime = _runtime(tmp_path)
    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        text = runtime._phone_menu_text()
    assert "缺少 OpenAI API Key" in text
    assert "通话配置" not in text


def test_live_phone_is_enabled_by_default():
    assert GlobalConfig(authorized_id=1).live_voice_v1 is True


def test_phone_menu_reports_ready_when_provider_key_exists(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.secrets = {"openai_api_key": "test-key"}
    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        text = runtime._phone_menu_text()
    assert "可以拨打" in text
    assert "缺少 OpenAI API Key" not in text


def test_phone_menu_reports_explicit_instance_opt_out(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.global_config.live_voice_v1 = False
    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        text = runtime._phone_menu_text()
    assert "本实例已关闭实时电话" in text


def test_phone_menu_uses_localized_labels_in_chinese(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.phone_manager.set_voice("willow")

    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        text = runtime._phone_menu_text()
        button_labels = [
            button.text
            for row in runtime._phone_keyboard().inline_keyboard
            for button in row
        ]
        voice_labels = [
            button.text
            for row in runtime._phone_keyboard("voice").inline_keyboard[:-1]
            for button in row
        ]

    assert "<b>语言</b> · 自动跟随" in text
    assert "<b>说话风格</b> · 自然" in text
    assert "<b>PCM 人格</b> · 已安全投影" in text
    assert "这是当前代理的工作区设置" in text
    assert "Willow · 女性声线" in text
    assert button_labels == [
        "服务商",
        "模型",
        "声音",
        "语言",
        "说话风格",
        "重置电话设置",
    ]
    assert "phone." not in text
    assert all("phone." not in label for label in button_labels)
    assert "Marin" in voice_labels
    assert "Quartz · 女性声线" in voice_labels
    assert "Ripple · 男性声线" in voice_labels
    assert "✓ Willow · 女性声线" in voice_labels
    assert all("phone." not in label for label in voice_labels)


@pytest.mark.asyncio
async def test_phone_command_persists_voice_and_custom_style(tmp_path):
    runtime = _runtime(tmp_path)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=7))

    await runtime.cmd_phone(update, SimpleNamespace(args=["voice", "willow"]))
    await runtime.cmd_phone(
        update,
        SimpleNamespace(args=["instructions", "Speak", "a", "little", "slower."]),
    )

    state = runtime.phone_manager.get_state()
    assert state["voice"] == "willow"
    assert state["style_instructions"] == "Speak a little slower."
    rendered = runtime._reply_text.await_args.args[1]
    assert "Willow" in rendered
    assert "Speak a little slower." in rendered


@pytest.mark.asyncio
async def test_phone_callback_changes_style_and_returns_to_summary(tmp_path):
    runtime = _runtime(tmp_path)

    class Query:
        data = "phone:style:calm"
        from_user = SimpleNamespace(id=7)

        def __init__(self):
            self.edits = []
            self.answers = []

        async def edit_message_text(self, text, **kwargs):
            self.edits.append((text, kwargs))

        async def answer(self, text=None, **kwargs):
            self.answers.append((text, kwargs))

    query = Query()
    await runtime.callback_phone(SimpleNamespace(callback_query=query), SimpleNamespace())

    assert runtime.phone_manager.get_state()["style"] == "calm"
    assert len(query.edits) == 1
    assert query.edits[0][1]["parse_mode"] == "HTML"
    assert query.answers
