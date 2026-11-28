"""Behavioural coverage for the P1 high-frequency menu copy.

These assertions pin the rendered result users actually see: localized
reasoning-level labels and plain-language cards. They intentionally avoid
word-list or regex guards.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import model_fallback_policy as policy_owner
from orchestrator import runtime_her_fallback
from orchestrator import runtime_menu_views
from orchestrator import runtime_model_selection
from orchestrator import ui_language
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.her_v2.models import effort_display_label


class _EffortHost:
    """Minimal host exposing the runtime surface the effort card needs."""

    config = SimpleNamespace(active_backend="her-v3")

    def _get_available_efforts(self):
        return ["zero", "low", "medium", "high", "xhigh", "max"]

    def _get_current_effort(self):
        return "max"

    def _is_binary_reasoning(self):
        return False

    def get_current_provider(self):
        return "openrouter"

    def get_current_model(self):
        return "deepseek-chat"

    def _effort_display_value(self, effort):
        return FlexibleAgentRuntime._effort_display_value(self, effort)


def _flat_buttons(keyboard) -> list[str]:
    return [button.text for row in keyboard.inline_keyboard for button in row]


def test_effort_label_follows_interface_language() -> None:
    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        assert effort_display_label("xhigh") == "极高"
        assert effort_display_label("max") == "最大"
    with ui_language.language_scope(SimpleNamespace(), locale="en"):
        assert effort_display_label("xhigh") == "Extra High"
        assert effort_display_label("max", include_value=True) == "Maximum (max)"


def test_effort_choices_render_localized_levels() -> None:
    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        labels = _flat_buttons(FlexibleAgentRuntime._effort_keyboard(_EffortHost()))
    assert labels == ["关闭", "低", "中", "高", "极高", "✓ 最大"]


def test_effort_card_shows_the_current_level_in_plain_words() -> None:
    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        text = FlexibleAgentRuntime._build_effort_followup_text(_EffortHost())
    assert "<code>最大</code>" in text
    assert "推理档位" in text
    assert "请选择推理档位" in text


def test_model_card_shows_the_localized_reasoning_level() -> None:
    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        text = runtime_menu_views.her_v3_model_menu_text(
            provider="openrouter", model="deepseek-chat", effort="max"
        )
    assert "推理档位" in text
    assert "<code>最大</code>" in text


@pytest.mark.parametrize(
    "render",
    [
        lambda: runtime_menu_views.her_v2_provider_menu_text(
            current_provider="openrouter", available_count=3
        ),
        lambda: runtime_menu_views.her_v2_provider_unavailable_text(backend="codex"),
    ],
)
def test_provider_cards_use_the_localized_title(render) -> None:
    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        text = render()
    assert "<b>模型服务方</b>" in text


def test_think_card_names_the_three_separate_switches() -> None:
    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        text = runtime_menu_views.thinking_output_text(
            enabled=True,
            her_backend=True,
            reasoning_available=True,
            commentary_available=True,
        )
    assert "<b>思考过程</b>" in text
    assert "<code>可用</code>" in text
    assert "/commentary" in text
    assert "/verbose" in text


def test_fallback_menu_buttons_speak_plain_chinese() -> None:
    policy = policy_owner.FallbackPolicy()
    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        labels = _flat_buttons(
            runtime_her_fallback.fallback_menu_keyboard(SimpleNamespace(), policy=policy)
        )
    assert labels[:4] == ["✓ 关闭", "只重试", "先问我", "自动切换"]
    assert labels[4:8] == ["目标", "预算", "来源", "隐私"]


def test_fallback_card_explains_the_mode_in_plain_chinese() -> None:
    runtime = SimpleNamespace(backend_manager=SimpleNamespace(privacy_level=1))
    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        text = runtime_her_fallback.fallback_menu_text(
            runtime, policy=policy_owner.FallbackPolicy()
        )
    assert "额度不足" in text
    assert "不换备用模型" in text


def test_reboot_title_names_agents_not_internal_functions() -> None:
    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        assert ui_language.tr("reboot.title") == "重启代理"
    with ui_language.language_scope(SimpleNamespace(), locale="en"):
        assert ui_language.tr("reboot.title") == "Reboot agents"


def test_effort_label_covers_boolean_thinking_models() -> None:
    with ui_language.language_scope(SimpleNamespace(), locale="zh-CN"):
        assert effort_display_label("enabled") == "开启"
        assert effort_display_label("off") == "关闭"
        assert effort_display_label("n/a") == "n/a"
        assert effort_display_label("") == "默认"
    with ui_language.language_scope(SimpleNamespace(), locale="en"):
        assert effort_display_label("enabled") == "On"
        assert effort_display_label("off") == "Off"


class _BinaryModelHost:
    """Host for the real /model entry with boolean thinking configured."""

    config = SimpleNamespace(active_backend="her-v2")
    backend_manager = SimpleNamespace(
        agent_mode="fixed",
        current_backend=SimpleNamespace(
            _v2_config=SimpleNamespace(
                profiles={"main": SimpleNamespace(engine="ollama", model="llama3")}
            )
        ),
        get_her_v3_target=lambda: SimpleNamespace(provider="ollama", model="llama3"),
        _her_v3_provider_option=lambda provider: {"models": ["llama3"]},
    )

    def __init__(self) -> None:
        self.replies: list[str] = []

    def _is_authorized_user(self, user_id) -> bool:
        return True

    def _get_current_effort(self) -> str:
        return "enabled"

    def _get_available_efforts(self) -> list[str]:
        return ["enabled", "off"]

    def _is_binary_reasoning(self) -> bool:
        return True

    def _effort_display_value(self, effort: str) -> str:
        return FlexibleAgentRuntime._effort_display_value(self, effort)

    def get_current_provider(self) -> str:
        return "ollama"

    def get_current_model(self) -> str:
        return "llama3"

    async def _reply_text(self, update, text, **kwargs) -> None:
        self.replies.append(text)


@pytest.mark.asyncio
async def test_model_entry_opens_for_binary_thinking_model() -> None:
    host = _BinaryModelHost()
    with ui_language.language_scope(host, locale="zh-CN"):
        await runtime_model_selection.cmd_model(
            host,
            SimpleNamespace(effective_user=SimpleNamespace(id=1)),
            SimpleNamespace(args=[]),
        )
    assert len(host.replies) == 1
    assert "<code>开启</code>" in host.replies[0]


class _EffortEntryHost(_EffortHost):
    config = SimpleNamespace(active_backend="her-v2")
    backend_manager = SimpleNamespace(current_backend=object())

    def __init__(self) -> None:
        self.replies: list[str] = []

    def _is_authorized_user(self, user_id) -> bool:
        return True

    def _set_active_effort(self, value: str) -> None:
        self.active = value

    def _effort_keyboard(self, current_effort=None, *, source=None):
        return FlexibleAgentRuntime._effort_keyboard(self, current_effort, source=source)

    def get_current_model(self) -> str:
        return "deepseek-chat"

    async def _reply_text(self, update, text, **kwargs) -> None:
        self.replies.append(text)


@pytest.mark.asyncio
async def test_effort_entry_card_and_echo_use_localized_levels() -> None:
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
    host = _EffortEntryHost()
    with ui_language.language_scope(host, locale="zh-CN"):
        await FlexibleAgentRuntime.cmd_effort(host, update, SimpleNamespace(args=[]))
    assert "<code>最大</code>" in host.replies[-1]

    host = _EffortEntryHost()
    with ui_language.language_scope(host, locale="zh-CN"):
        await FlexibleAgentRuntime.cmd_effort(host, update, SimpleNamespace(args=["max"]))
    assert host.replies[-1] == "推理档位已切换为：最大"


@pytest.mark.asyncio
async def test_effort_button_echo_uses_localized_level() -> None:
    from unittest.mock import AsyncMock

    host = _EffortEntryHost()
    host.error_logger = SimpleNamespace(exception=lambda *args: None)
    query = SimpleNamespace(
        data="effort:max",
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    with ui_language.language_scope(host, locale="zh-CN"):
        await runtime_model_selection.callback_model(
            host, SimpleNamespace(callback_query=query), SimpleNamespace()
        )
    query.edit_message_text.assert_awaited_once()
    assert query.edit_message_text.await_args.args[0] == "推理档位已切换为：最大"


def test_think_card_does_not_promise_an_unavailable_stream() -> None:
    host = SimpleNamespace(
        _think=True,
        config=SimpleNamespace(active_backend="codex-cli"),
        backend_manager=SimpleNamespace(
            current_backend=SimpleNamespace(
                capabilities=SimpleNamespace(
                    supports_thinking_stream=False,
                    supports_commentary_stream=False,
                )
            )
        ),
        _is_her_commentary_backend=lambda: False,
    )
    with ui_language.language_scope(host, locale="zh-CN"):
        text = FlexibleAgentRuntime._think_menu_text(host)
    assert "未提供" in text
    assert "这个开关对它无效" in text
    assert "会显示模型的思考过程" not in text
    with ui_language.language_scope(host, locale="en"):
        text = FlexibleAgentRuntime._think_menu_text(host)
    assert "this switch has no effect" in text
    assert "Shows the model's thinking output" not in text
# HASHI-Fingerprint: hfp_1d39258c447a4ea2b89bce6ffbca20b7
