from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest

from adapters.stream_events import KIND_FILE_READ, StreamEvent
from orchestrator import (
    runtime_command_binding,
    runtime_groups,
    runtime_menu_views,
    ui_language,
)
from orchestrator.activity_digest import ActivityDigest
from orchestrator.command_ui import back_label, card_title, help_menu_text, refresh_label
from orchestrator.config import GlobalConfig
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.runtime_delivery import format_backend_error_for_user
from orchestrator.voice_manager import VoiceManager


def _runtime(tmp_path, *, user_id: int = 42):
    return SimpleNamespace(
        name="zelda",
        global_config=GlobalConfig(
            authorized_id=user_id,
            bridge_home=tmp_path,
            project_root=tmp_path,
        ),
        telegram_connected=True,
    )


def _update(*, user_id: int = 42, chat_id: int = 42):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        callback_query=None,
    )


def test_catalogs_are_complete_and_keep_formal_chinese_agent_term() -> None:
    assert ui_language.validate_catalogs() == []
    chinese = ui_language.load_catalog("zh-CN")

    assert chinese.commands["agents"] == "查看和管理代理"
    assert chinese.strings["reboot.all_active"] == "所有已启用的代理"
    assert chinese.strings["reboot.all_running"] == "所有正在运行的代理"
    assert all(
        "agent"
        not in re.sub(
            r"<code>.*?</code>|\{[^{}]+\}|/[a-z][a-z0-9_-]*",
            "",
            value,
            flags=re.DOTALL,
        ).casefold()
        for mapping in (chinese.strings, chinese.commands, chinese.titles)
        for value in mapping.values()
    )


def test_locale_aliases_and_english_fallback() -> None:
    assert ui_language.normalize_locale("zh") == "zh-CN"
    assert ui_language.normalize_locale("简体中文") == "zh-CN"
    assert ui_language.normalize_locale("EN-gb") == "en"
    assert ui_language.tr("missing.key", locale="zh-CN") == "missing.key"


def test_user_language_preference_is_shared_across_agent_runtimes(tmp_path) -> None:
    first = _runtime(tmp_path)
    second = _runtime(tmp_path)
    update = _update()

    ui_language.set_preferred_locale(first, "zh", update)

    assert ui_language.preferred_locale(second, update) == "zh-CN"
    assert ui_language.saved_user_locales(second) == {"42": "zh-CN"}
    ui_language.reset_preferred_locale(second, update)
    assert ui_language.preferred_locale(first, update) == "en"


def test_session_owner_id_uses_same_user_language_preference(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    ui_language.set_preferred_locale(runtime, "zh-CN", actor_id=42)

    assert ui_language.preferred_locale(runtime, actor_id="user:42") == "zh-CN"
    assert (
        ui_language.actor_id_from_update(
            SimpleNamespace(_hashi_session_owner_id="user:42")
        )
        == "42"
    )


def test_instance_default_is_used_when_user_has_no_saved_preference(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime.global_config.ui_language = "zh-CN"

    assert ui_language.preferred_locale(runtime, _update()) == "zh-CN"


def test_common_cards_and_navigation_follow_active_language(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    with ui_language.language_scope(runtime, locale="zh-CN"):
        assert back_label() == "← 返回"
        assert refresh_label() == "↻ 刷新"
        assert card_title("🤖", "Hashi agents").startswith("🤖 <b>HASHI 代理</b>")


def test_chinese_menu_bodies_and_buttons_do_not_keep_english_shells(tmp_path) -> None:
    class Slots:
        def list_slots(self):
            return [
                {"slot": str(index), "active": index <= 2, "text": "已配置" if index <= 6 else ""}
                for index in range(1, 11)
            ]

    class Directory:
        def list_groups(self):
            return {}

    runtime = object.__new__(FlexibleAgentRuntime)
    voice = VoiceManager(tmp_path / "workspace", tmp_path / "media")
    runtime.voice_manager = voice

    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        sys_text = runtime_menu_views.sys_slots_text(Slots())
        group_text, group_keyboard = runtime_groups.group_list_view(Directory())
        voice_text = voice.voice_menu_text()
        voice_keyboard = runtime._voice_keyboard().inline_keyboard
        wrapper_text = runtime._wrapper_status_text({}, {"1": "保持简洁"})

    assert "<b>当前</b> · <code>2</code> 个已启用" in sys_text
    assert "<b>已配置</b> · <code>6/10</code>" in sys_text
    assert "<b>作用范围</b> · 仅限当前代理" in sys_text
    assert "<b>槽位</b>" in sys_text
    for old_label in ("Current", "Configured", "Scope", "Changes", "SLOTS"):
        assert old_label not in sys_text

    assert "<b>当前</b> · <code>0</code> 个群组" in group_text
    assert "尚未定义任何群组。" in group_text
    assert group_keyboard.inline_keyboard[-1][0].text == "新建群组"

    assert "<b>语音风格</b> · 自定义" in voice_text
    assert "<b>语音生成方式</b>" in voice_text
    assert "将原始音频直接交给音频模型" in voice_text
    assert "<b>原生语音回复形式</b> · 语音 + 文字 · 仅控制原生语音回复" in voice_text
    assert [button.text for button in voice_keyboard[0]] == [
        "原生音频模型",
        "文字模型 + TTS",
    ]

    assert "<b>当前</b> · 核心模型" in wrapper_text
    assert "<b>角色 / 风格槽位</b>" in wrapper_text
    assert "Model changes" not in wrapper_text


def test_help_and_telegram_command_menu_use_chinese_catalog(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    commands = runtime_command_binding.get_flexible_bot_commands(
        runtime,
        locale="zh-CN",
    )
    descriptions = {item.command: item.description for item in commands}

    assert descriptions["language"] == "选择界面语言"
    assert descriptions["agents"] == "查看和管理代理"
    text = help_menu_text(
        agent_name="zelda",
        agent_type="flex",
        commands=commands,
        locale="zh-CN",
    )
    assert text.startswith("⚔️ <b>HASHI 命令中心</b>")
    assert "⚡ <b>常用命令</b>" in text
    assert "个可用命令" in text


@pytest.mark.asyncio
async def test_saved_language_command_menu_is_restored_as_chat_scope(tmp_path) -> None:
    calls = []

    class Bot:
        async def set_my_commands(self, commands, **kwargs):
            calls.append((commands, kwargs))

    runtime = _runtime(tmp_path)
    runtime.app = SimpleNamespace(bot=Bot())
    ui_language.set_preferred_locale(runtime, "zh-CN", actor_id=42)

    await runtime_command_binding.register_flexible_bot_commands(runtime)

    assert calls[0][1] == {}
    scoped = [call for call in calls if "scope" in call[1]]
    assert len(scoped) == 1
    assert scoped[0][1]["scope"].chat_id == 42
    descriptions = {item.command: item.description for item in scoped[0][0]}
    assert descriptions["reboot"] == "热重启代理"


@pytest.mark.asyncio
async def test_language_selection_persists_before_background_menu_refresh(
    tmp_path,
) -> None:
    calls = []

    class Bot:
        async def set_my_commands(self, commands, **kwargs):
            calls.append((commands, kwargs))

    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "zelda"
    runtime.global_config = GlobalConfig(
        authorized_id=42,
        bridge_home=tmp_path,
        project_root=tmp_path,
    )
    runtime.telegram_connected = True
    runtime.app = SimpleNamespace(bot=Bot())
    runtime.orchestrator = SimpleNamespace(runtimes=[runtime])
    update = _update()

    selected, notice, failures = await runtime._apply_ui_language(
        update,
        requested="zh",
    )

    assert selected == "zh-CN"
    assert notice == "界面语言已切换为简体中文。"
    assert failures == 0
    assert ui_language.preferred_locale(runtime, update) == "zh-CN"
    assert calls == []

    runtime._schedule_language_menu_sync(update, chat_id=42, locale=selected)
    await asyncio.gather(*runtime._language_menu_sync_tasks)

    assert calls[-1][1]["scope"].chat_id == 42
    descriptions = {item.command: item.description for item in calls[-1][0]}
    assert descriptions["agents"] == "查看和管理代理"


@pytest.mark.asyncio
async def test_language_menu_sync_is_serialized_and_latest_selection_wins(
    tmp_path, monkeypatch
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def sync(_runtime, *, chat_id, locale):
        calls.append((chat_id, locale))
        if locale == "zh-CN":
            started.set()
            await release.wait()
        return runtime_command_binding.CommandMenuSyncResult(1, 1, ())

    monkeypatch.setattr(runtime_command_binding, "sync_user_command_menus", sync)
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "zelda"
    runtime.global_config = GlobalConfig(
        authorized_id=42, bridge_home=tmp_path, project_root=tmp_path
    )
    runtime._reply_text = lambda *_args, **_kwargs: None
    update = _update()

    runtime._schedule_language_menu_sync(update, chat_id=42, locale="zh-CN")
    await started.wait()
    runtime._schedule_language_menu_sync(update, chat_id=42, locale="en")
    release.set()
    await asyncio.gather(*tuple(runtime._language_menu_sync_tasks))

    assert calls == [(42, "zh-CN"), (42, "en")]


def test_telegram_activity_can_be_chinese_while_terminal_default_stays_english() -> None:
    digest = ActivityDigest()
    digest.record(
        StreamEvent(
            kind=KIND_FILE_READ,
            summary="read",
            file_path="orchestrator/config.py",
        )
    )

    assert digest.phase_label_for(locale="zh-CN") == "执行"
    assert digest.render_lines(locale="zh-CN") == ["🔎 检查了 1 个文件"]
    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        assert digest.phase_label == "Execution"
        assert digest.render_lines() == ["🔎 Inspected 1 file"]


def test_backend_wrapper_is_localized_but_exact_provider_error_is_unchanged() -> None:
    raw = "[PROVIDER_BAD_REQUEST] upstream rejected request_id=abc"

    text = format_backend_error_for_user("her-v2", raw, locale="zh-CN")

    assert text.startswith("错误详情：")
    assert raw in text



def test_scheduler_notice_is_english_by_default_and_chinese_when_selected() -> None:
    from orchestrator.scheduler_recovery import render_notice

    batch = {
        "batch_id": "recovery-1",
        "items": [
            {
                "task_id": "hourly-test",
                "kind": "heartbeat",
                "description": "test task",
                "interval_seconds": 3600,
                "missed_count": 1,
                "replay_limit": 1,
                "due_at": [1.0],
                "first_due_at": 1.0,
                "last_due_at": 1.0,
            }
        ],
    }

    assert render_notice(batch).startswith("⏰ HASHI offline recovery")
    chinese = render_notice(batch, locale="zh-CN")
    assert chinese.startswith("⏰ HASHI 离线恢复")
    assert "内容：test task" in chinese
    assert "全部补跑" in chinese


@pytest.mark.asyncio
async def test_backend_busy_notice_uses_selected_ui_language():
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    runtime = SimpleNamespace(
        config=SimpleNamespace(allowed_backends=[{"engine": "codex-cli"}]),
        _evaluate_enterprise_policy=lambda *a, **kw: SimpleNamespace(allowed=True),
        _backend_busy=lambda: True,
    )
    with ui_language.language_scope(runtime, locale="zh-CN"):
        ok, message = await FlexibleAgentRuntime._switch_backend_mode(runtime, 0, "codex-cli")
    assert not ok
    assert "正在运行或排队" in message
    assert "Backend switch" not in message


@pytest.mark.asyncio
async def test_backend_busy_callback_keeps_selection_card_in_chinese():
    from unittest.mock import AsyncMock

    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.config = SimpleNamespace(allowed_backends=[{"engine": "codex-cli"}])
    runtime._is_authorized_user = lambda _user: True
    runtime._evaluate_enterprise_policy = lambda *a, **kw: SimpleNamespace(allowed=True)
    runtime._backend_busy = lambda: True
    runtime.error_logger = SimpleNamespace(exception=lambda *a: None)
    query = SimpleNamespace(
        data="bmodel:codex-cli:p:gpt-5.4", from_user=SimpleNamespace(id=42),
        message=SimpleNamespace(chat_id=42), answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    with ui_language.language_scope(runtime, locale="zh-CN"):
        await runtime.callback_model(SimpleNamespace(callback_query=query), SimpleNamespace())
    query.edit_message_text.assert_not_awaited()
    query.answer.assert_awaited_once()
    assert query.answer.await_args.kwargs["show_alert"] is True
    assert "正在运行或排队" in query.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_backend_success_notice_reports_saved_mode_in_selected_language(monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from orchestrator import runtime_session

    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.config = SimpleNamespace(allowed_backends=[{"engine": "codex-cli"}])
    runtime.backend_manager = SimpleNamespace(
        agent_mode="fixed", switch_backend=AsyncMock(return_value=True),
        current_backend=SimpleNamespace(capabilities=SimpleNamespace(supports_sessions=True)),
    )
    runtime._evaluate_enterprise_policy = lambda *a, **kw: SimpleNamespace(allowed=True)
    runtime._backend_busy = lambda: False
    runtime._sync_workzone_to_backend_config = Mock()
    runtime._clear_handoff_state = Mock()
    runtime._arm_session_primer = Mock()
    runtime.get_current_model = lambda: "gpt-5.4"
    runtime.get_current_provider = lambda: None
    runtime._get_current_effort = lambda: "high"
    monkeypatch.setattr(runtime_session, "current_session", lambda *a, **kw: {"session_id": "test"})
    monkeypatch.setattr(runtime_session, "apply_session_workzones", lambda *a, **kw: None)
    with ui_language.language_scope(runtime, locale="zh-CN"):
        ok, message = await runtime._switch_backend_mode(42, "codex-cli")
    assert ok
    assert "模式: fixed" in message
    assert "模型: gpt-5.4" in message
    assert "不携带交接上下文" in message
    assert "Backend switched" not in message


@pytest.mark.asyncio
async def test_mode_notice_uses_escaped_html_and_keeps_backend_available():
    from orchestrator import runtime_mode
    from unittest.mock import AsyncMock, Mock

    runtime = SimpleNamespace(
        config=SimpleNamespace(active_backend="test<&>"),
        backend_manager=SimpleNamespace(
            agent_mode="flex", _save_state=Mock(),
            current_backend=SimpleNamespace(capabilities=SimpleNamespace(supports_sessions=False)),
        ),
        _reply_text=AsyncMock(),
    )
    with ui_language.language_scope(runtime, locale="en"):
        await runtime_mode.switch_mode_from_command(runtime, None, "fixed")
        call = runtime._reply_text.await_args
        assert call.kwargs["parse_mode"] == "HTML"
        assert "test&lt;&amp;&gt;" in call.args[1]
        runtime.backend_manager.current_backend.capabilities.supports_sessions = True
        await runtime_mode.switch_mode_from_command(runtime, None, "fixed")
        assert "<code>/backend</code> remains available" in runtime._reply_text.await_args.args[1]
