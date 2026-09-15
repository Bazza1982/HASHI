from __future__ import annotations

from types import SimpleNamespace

from orchestrator import ui_language
from orchestrator.runtime_her_fallback import (
    fallback_menu_keyboard,
    fallback_menu_text,
)


def _runtime(*, enabled: bool = True):
    primary = SimpleNamespace(provider="deepseek-api", model="deepseek-flash")
    selected = SimpleNamespace(
        fallback_enabled=enabled,
        fallback_targets={
            1: {
                "light": SimpleNamespace(
                    provider="deepseek-api",
                    model="deepseek-v4-pro",
                )
            },
            2: {
                "light": SimpleNamespace(
                    provider="openrouter-api",
                    model="deepseek/deepseek-v4-flash",
                )
            },
        },
        target_for_slot=lambda _slot: primary,
    )
    manager = SimpleNamespace(
        get_her_v2_configuration=lambda: selected,
        get_her_v2_provider_options=lambda: [
            {
                "name": "deepseek",
                "engine": "deepseek-api",
                "label": "DeepSeek",
                "models": ["deepseek-flash", "deepseek-v4-pro"],
                "available": True,
            },
            {
                "name": "openrouter",
                "engine": "openrouter-api",
                "label": "OpenRouter",
                "models": ["deepseek/deepseek-v4-flash"],
                "available": True,
            },
        ],
    )
    return SimpleNamespace(backend_manager=manager)


def test_fallback_menu_and_command_description_follow_ui_locale():
    runtime = _runtime()

    with ui_language.language_scope(None, locale="en"):
        english = fallback_menu_text(runtime)
        english_description = ui_language.command_description("fallback", "fallback")
    with ui_language.language_scope(None, locale="zh-CN"):
        chinese = fallback_menu_text(runtime)
        chinese_description = ui_language.command_description("fallback", "fallback")

    assert "HER V2 PROVIDER FALLBACK" in english
    assert "Model unavailable" not in english
    assert "HER V2 供应商 FALLBACK" in chinese
    assert english_description == "Configure HER v2 provider fallback"
    assert chinese_description == "配置 HER v2 供应商 fallback"


def test_fallback_keyboard_callback_payloads_fit_telegram_limit():
    with ui_language.language_scope(None, locale="zh-CN"):
        markup = fallback_menu_keyboard(_runtime())

    callback_data = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]
    assert callback_data
    assert all(len(value.encode("utf-8")) <= 64 for value in callback_data)
    assert "fallback:slot:1:light" in callback_data
    assert "fallback:slot:2:pro" in callback_data
