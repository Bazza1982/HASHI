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
# HASHI-Fingerprint: hfp_1d39258c447a4ea2b89bce6ffbca20b7
