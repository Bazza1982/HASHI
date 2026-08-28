from __future__ import annotations

from types import SimpleNamespace

import pytest

from adapters.stream_events import KIND_FILE_READ, StreamEvent
from orchestrator import runtime_command_binding, ui_language
from orchestrator.activity_digest import ActivityDigest
from orchestrator.command_ui import back_label, card_title, help_menu_text, refresh_label
from orchestrator.config import GlobalConfig
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.runtime_delivery import format_backend_error_for_user


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
        "agent" not in value.casefold()
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
async def test_language_selection_persists_and_refreshes_all_live_agent_menus(
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
    assert calls[-1][1]["scope"].chat_id == 42
    descriptions = {item.command: item.description for item in calls[-1][0]}
    assert descriptions["agents"] == "查看和管理代理"


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

    assert text.startswith("后端返回的准确错误：")
    assert raw in text


@pytest.mark.asyncio
async def test_reboot_system_message_uses_formal_chinese_agent_word() -> None:
    replies = []
    restart_requests = []

    async def reply_text(_update, text, **_kwargs):
        replies.append(text)

    runtime = SimpleNamespace(
        name="zelda",
        orchestrator=SimpleNamespace(
            request_restart=lambda **kwargs: restart_requests.append(kwargs),
        ),
        _is_authorized_user=lambda _user_id: True,
        _reply_text=reply_text,
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=42))

    with ui_language.language_scope(runtime, locale="zh-CN"):
        await FlexibleAgentRuntime.cmd_reboot(
            runtime,
            update,
            SimpleNamespace(args=["max"]),
        )

    assert replies == ["正在重启所有已启用的代理……"]
    assert restart_requests == [
        {"mode": "max", "agent_name": "zelda", "agent_number": None}
    ]


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
