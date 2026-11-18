from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.privacy_levels import PrivacyLevel
from orchestrator.runtime_privacy import (
    _verify_level_two_detector,
    callback_privacy,
    cmd_privacy,
    privacy_status_text,
)


def _runtime(*, level: PrivacyLevel = PrivacyLevel.PROVIDER_TRUST):
    manager = SimpleNamespace(
        privacy_level=level,
        set_privacy_level=AsyncMock(),
    )

    def set_level(requested, *, expected_current_level=None):
        if expected_current_level is not None and int(manager.privacy_level) != expected_current_level:
            raise ValueError("privacy setting changed; refresh before applying")
        manager.privacy_level = PrivacyLevel(int(requested))
        return manager.privacy_level

    manager.set_privacy_level = set_level
    return SimpleNamespace(
        backend_manager=manager,
        config=SimpleNamespace(active_backend="codex-cli"),
        global_config=SimpleNamespace(authorized_id=1),
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

    assert "<b>Current</b> · <b>LEVEL 0</b> · Privacy Off" in status
    assert "Privacy framework bypassed" in status
    assert "Default: Level 1 — Provider Trust" in status
    assert "2  Trial · local PII masking · HERV3 only" in status
    assert "🔒 5  Local · nothing leaves the environment" in status


@pytest.mark.asyncio
@pytest.mark.parametrize(("initial", "requested", "expected"), [
    (PrivacyLevel.PROVIDER_TRUST, "0", PrivacyLevel.OFF),
    (PrivacyLevel.BASIC_REDACTION, "1", PrivacyLevel.PROVIDER_TRUST),
])
async def test_explicit_authorized_privacy_command_applies_downgrade_once(
    initial, requested, expected,
) -> None:
    runtime = _runtime(level=initial)

    await cmd_privacy(runtime, _update(), SimpleNamespace(args=[requested]))

    assert runtime.backend_manager.privacy_level is expected
    message = runtime._reply_text.await_args.args[1]
    assert f"Privacy changed to Level {requested}" in message
    assert "Confirm this privacy downgrade" not in message


@pytest.mark.asyncio
async def test_privacy_menu_downgrade_still_explains_effect_before_confirmation() -> None:
    runtime = _runtime()
    update = _callback_update("privacy:set:0")

    await callback_privacy(runtime, update, SimpleNamespace())

    message = update.callback_query.edit_message_text.await_args.args[0]
    keyboard = update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
    assert "Confirm this privacy downgrade" in message
    assert keyboard.inline_keyboard[0][0].callback_data == "privacy:confirm:0"
    assert runtime.backend_manager.privacy_level is PrivacyLevel.PROVIDER_TRUST


@pytest.mark.asyncio
async def test_unauthorized_explicit_privacy_command_does_not_change_policy() -> None:
    runtime = _runtime()
    runtime._is_authorized_user = lambda _user_id: False

    await cmd_privacy(runtime, _update(), SimpleNamespace(args=["0"]))

    assert runtime.backend_manager.privacy_level is PrivacyLevel.PROVIDER_TRUST
    runtime._reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorized_delegate_still_receives_downgrade_review() -> None:
    runtime = _runtime()
    runtime.global_config.authorized_id = 2

    await cmd_privacy(runtime, _update(), SimpleNamespace(args=["0"]))

    assert runtime.backend_manager.privacy_level is PrivacyLevel.PROVIDER_TRUST
    assert "Confirm this privacy downgrade" in runtime._reply_text.await_args.args[1]


@pytest.mark.asyncio
async def test_frontend_owner_can_issue_explicit_downgrade_without_telegram() -> None:
    from orchestrator.frontend_command_context import bind_frontend_command_principal

    runtime = _runtime()
    runtime.global_config.authorized_id = 0
    update = SimpleNamespace(effective_user=SimpleNamespace(id="owner:authenticated"))
    with bind_frontend_command_principal(runtime, "owner:authenticated"):
        await cmd_privacy(runtime, update, SimpleNamespace(args=["0"]))

    assert runtime.backend_manager.privacy_level is PrivacyLevel.OFF


@pytest.mark.asyncio
async def test_scoped_delegate_cannot_inherit_primary_command_authority(monkeypatch) -> None:
    runtime = _runtime()
    runtime._telegram_scoped_access_enabled = True
    monkeypatch.setattr(
        "orchestrator.telegram_actor_scope.runtime_snapshot",
        lambda _runtime: SimpleNamespace(primary_user_id=2, effective_user_ids={1, 2}),
    )

    await cmd_privacy(runtime, _update(), SimpleNamespace(args=["0"]))

    assert runtime.backend_manager.privacy_level is PrivacyLevel.PROVIDER_TRUST
    assert "Confirm this privacy downgrade" in runtime._reply_text.await_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(("granted", "expected"), [
    (True, PrivacyLevel.OFF),
    (False, PrivacyLevel.PROVIDER_TRUST),
])
async def test_scoped_primary_needs_current_telegram_grant(
    monkeypatch, granted, expected,
) -> None:
    runtime = _runtime()
    runtime._telegram_scoped_access_enabled = True
    monkeypatch.setattr(
        "orchestrator.telegram_actor_scope.runtime_snapshot",
        lambda _runtime: SimpleNamespace(
            primary_user_id=1, effective_user_ids={1} if granted else set(),
        ),
    )

    await cmd_privacy(runtime, _update(), SimpleNamespace(args=["0"]))

    assert runtime.backend_manager.privacy_level is expected


@pytest.mark.asyncio
async def test_expired_frontend_binding_cannot_confirm_downgrade() -> None:
    from orchestrator.frontend_command_context import bind_frontend_command_principal

    runtime = _runtime()
    update = SimpleNamespace(effective_user=SimpleNamespace(id="owner:authenticated"))
    with bind_frontend_command_principal(runtime, "owner:authenticated"):
        pass
    await cmd_privacy(runtime, update, SimpleNamespace(args=["0"]))

    assert runtime.backend_manager.privacy_level is PrivacyLevel.PROVIDER_TRUST
    assert "Confirm this privacy downgrade" in runtime._reply_text.await_args.args[1]



@pytest.mark.asyncio
async def test_unbound_frontend_identity_cannot_supply_owner_confirmation() -> None:
    runtime = _runtime()
    runtime.global_config.authorized_id = 0
    update = SimpleNamespace(effective_user=SimpleNamespace(id="owner:claimed"))

    await cmd_privacy(runtime, update, SimpleNamespace(args=["0"]))

    assert runtime.backend_manager.privacy_level is PrivacyLevel.PROVIDER_TRUST
    assert "Confirm this privacy downgrade" in runtime._reply_text.await_args.args[1]


@pytest.mark.asyncio
async def test_explicit_downgrade_does_not_overwrite_changed_privacy_setting() -> None:
    from orchestrator.privacy_levels import PrivacyPolicyError

    runtime = _runtime()
    def changed_before_save(_requested, *, expected_current_level=None):
        assert expected_current_level == 1
        runtime.backend_manager.privacy_level = PrivacyLevel.BASIC_REDACTION
        raise PrivacyPolicyError("privacy setting changed; refresh before applying")
    runtime.backend_manager.set_privacy_level = changed_before_save

    await cmd_privacy(runtime, _update(), SimpleNamespace(args=["0"]))

    assert runtime.backend_manager.privacy_level is PrivacyLevel.BASIC_REDACTION
    assert "not changed" in runtime._reply_text.await_args.args[1].lower()


@pytest.mark.asyncio
async def test_confirming_level_zero_changes_and_reports_full_details() -> None:
    runtime = _runtime()
    update = _callback_update("privacy:confirm:0")

    await callback_privacy(runtime, update, SimpleNamespace())

    assert runtime.backend_manager.privacy_level is PrivacyLevel.OFF
    message = update.callback_query.edit_message_text.await_args.args[0]
    assert "Privacy changed to Level 0" in message
    assert "<b>Current</b> · <b>LEVEL 0</b> · Privacy Off" in message
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
    assert "<b>Current</b> · <b>LEVEL 1</b> · Provider Trust" in message
    assert kwargs["parse_mode"] == "HTML"
    assert [button.text for button in keyboard.inline_keyboard[0]] == ["0 · Off", "✓ 1 · Trust"]
    assert [button.text for button in keyboard.inline_keyboard[1]] == [
        "2 · Trial",
        "🔒 3 · Strict",
    ]
    assert keyboard.inline_keyboard[1][0].callback_data == "privacy:set:2"


@pytest.mark.asyncio
async def test_privacy_level_two_requires_risk_acceptance() -> None:
    runtime = _runtime()
    runtime.config.active_backend = "openrouter-api"

    await cmd_privacy(runtime, _update(), SimpleNamespace(args=["2"]))

    message = runtime._reply_text.await_args.args[1]
    assert "Undetected PII may still be sent" in message
    keyboard = runtime._reply_text.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].callback_data == "privacy:accept:2"
    assert runtime.backend_manager.privacy_level is PrivacyLevel.PROVIDER_TRUST

    await callback_privacy(runtime, _callback_update("privacy:accept:2"), SimpleNamespace())
    assert runtime.backend_manager.privacy_level is PrivacyLevel.PROVIDER_TRUST


@pytest.mark.asyncio
async def test_herv3_level_two_acceptance_activates_trial(monkeypatch) -> None:
    runtime = _runtime()
    runtime.config.active_backend = "her-v2"
    update = _callback_update("privacy:accept:2")
    detector_check = AsyncMock()
    monkeypatch.setattr(
        "orchestrator.runtime_privacy._verify_level_two_detector", detector_check
    )

    await callback_privacy(runtime, update, SimpleNamespace())

    assert runtime.backend_manager.privacy_level is PrivacyLevel.BASIC_REDACTION
    detector_check.assert_awaited_once()
    message = update.callback_query.edit_message_text.await_args.args[0]
    assert "Local PII Masking (Trial)" in message
    assert "Detection can miss PII" in message


@pytest.mark.platform
@pytest.mark.asyncio
async def test_level_two_readiness_uses_local_model() -> None:
    if not os.getenv("HASHI_PRIVACY_FILTER_PYTHON"):
        pytest.skip("isolated privacy model interpreter is not configured")

    await _verify_level_two_detector()


@pytest.mark.asyncio
async def test_future_privacy_level_returns_framework_details() -> None:
    runtime = _runtime()

    await cmd_privacy(runtime, _update(), SimpleNamespace(args=["4"]))

    message = runtime._reply_text.await_args.args[1]
    assert "Privacy Level 4 is reserved" in message
    assert "verified private or single-tenant deployment" in message
