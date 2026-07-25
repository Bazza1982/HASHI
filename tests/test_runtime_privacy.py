from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.privacy_levels import PrivacyLevel
from orchestrator.runtime_privacy import (
    callback_privacy,
    cmd_privacy,
    privacy_status_text,
)


def _runtime(*, level: PrivacyLevel = PrivacyLevel.PROVIDER_TRUST):
    manager = SimpleNamespace(
        privacy_level=level,
        set_privacy_level=AsyncMock(),
    )

    def set_level(requested):
        manager.privacy_level = PrivacyLevel(int(requested))
        return manager.privacy_level

    manager.set_privacy_level = set_level
    return SimpleNamespace(
        backend_manager=manager,
        config=SimpleNamespace(active_backend="codex-cli"),
        _backend_busy=lambda: False,
        _is_authorized_user=lambda _user_id: True,
        _reply_text=AsyncMock(),
    )


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=1))


def _callback_update(data: str):
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(callback_query=query)


def test_privacy_status_explains_level_zero_and_default() -> None:
    runtime = _runtime(level=PrivacyLevel.OFF)

    status = privacy_status_text(runtime)

    assert "<b>LEVEL 0 · Privacy Off</b>" in status
    assert "Privacy framework bypassed" in status
    assert "Default: Level 1 — Provider Trust" in status
    assert "🔒 2  Basic · one filter · API only" in status
    assert "🔒 5  Local · nothing leaves the environment" in status


@pytest.mark.asyncio
async def test_privacy_zero_requires_confirmation_when_lowering() -> None:
    runtime = _runtime()

    await cmd_privacy(runtime, _update(), SimpleNamespace(args=["0"]))

    message = runtime._reply_text.await_args.args[1]
    keyboard = runtime._reply_text.await_args.kwargs["reply_markup"]
    assert "Confirm this privacy downgrade" in message
    assert keyboard.inline_keyboard[0][0].callback_data == "privacy:confirm:0"
    assert keyboard.inline_keyboard[1][0].callback_data == "privacy:menu"
    assert runtime.backend_manager.privacy_level is PrivacyLevel.PROVIDER_TRUST


@pytest.mark.asyncio
async def test_confirming_level_zero_changes_and_reports_full_details() -> None:
    runtime = _runtime()
    update = _callback_update("privacy:confirm:0")

    await callback_privacy(runtime, update, SimpleNamespace())

    assert runtime.backend_manager.privacy_level is PrivacyLevel.OFF
    message = update.callback_query.edit_message_text.await_args.args[0]
    assert "Privacy changed to Level 0" in message
    assert "<b>LEVEL 0 · Privacy Off</b>" in message
    assert update.callback_query.edit_message_text.await_args.kwargs["parse_mode"] == "HTML"
    update.callback_query.answer.assert_awaited_once_with("Privacy Level 0")


@pytest.mark.asyncio
async def test_privacy_menu_uses_compact_two_column_level_buttons() -> None:
    runtime = _runtime()

    await cmd_privacy(runtime, _update(), SimpleNamespace(args=[]))

    message = runtime._reply_text.await_args.args[1]
    kwargs = runtime._reply_text.await_args.kwargs
    keyboard = kwargs["reply_markup"]
    assert message.startswith("🛡️ <b>HASHI PRIVACY</b>")
    assert "<b>LEVEL 1 · Provider Trust</b>" in message
    assert kwargs["parse_mode"] == "HTML"
    assert [button.text for button in keyboard.inline_keyboard[0]] == ["0 · Off", "✓ 1 · Trust"]
    assert [button.text for button in keyboard.inline_keyboard[1]] == [
        "🔒 2 · Basic",
        "🔒 3 · Strict",
    ]


@pytest.mark.asyncio
async def test_privacy_level_two_is_visible_but_not_activatable_yet() -> None:
    runtime = _runtime()
    runtime.config.active_backend = "openrouter-api"

    await cmd_privacy(runtime, _update(), SimpleNamespace(args=["2"]))

    message = runtime._reply_text.await_args.args[1]
    assert "not available until its outbound protection is installed" in message
    assert runtime.backend_manager.privacy_level is PrivacyLevel.PROVIDER_TRUST


@pytest.mark.asyncio
async def test_future_privacy_level_returns_framework_details() -> None:
    runtime = _runtime()

    await cmd_privacy(runtime, _update(), SimpleNamespace(args=["4"]))

    message = runtime._reply_text.await_args.args[1]
    assert "Privacy Level 4 is reserved" in message
    assert "verified private or single-tenant deployment" in message
