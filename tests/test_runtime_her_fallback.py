"""Focused tests for the user-consented /fallback policy menu."""

from __future__ import annotations

import asyncio
import time

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import model_fallback_policy as policy_owner
from orchestrator import ui_language
from orchestrator.command_specs import COMMAND_SPEC_BY_NAME
from orchestrator.config_json import ConfigConflictError
from orchestrator import runtime_her_fallback as fallback_menu
from orchestrator.runtime_her_fallback import (
    callback_fallback,
    fallback_menu_keyboard,
    fallback_menu_text,
)


def _runtime(tmp_path, *, options=None, actor_free=True):
    manager = SimpleNamespace(
        privacy_level=1,
        get_her_v2_provider_options=lambda: list(
            options
            if options is not None
            else [
                {
                    "name": "deepseek",
                    "engine": "deepseek-api",
                    "label": "DeepSeek",
                    "models": ["deepseek-v4-flash"],
                    "available": True,
                },
                {
                    "name": "openrouter",
                    "engine": "openrouter-api",
                    "label": "OpenRouter",
                    "models": ["deepseek/deepseek-v4-flash"],
                    "available": True,
                },
                {
                    "name": "codex",
                    "engine": "codex-cli",
                    "label": "Codex",
                    "models": [],
                    "available": True,
                },
            ]
        ),
        get_her_v3_target=lambda: SimpleNamespace(
            provider="deepseek-api", model="deepseek-v4-flash"
        ),
    )
    runtime = SimpleNamespace(
        backend_manager=manager,
        global_config=SimpleNamespace(bridge_home=tmp_path, project_root=tmp_path),
        _is_authorized_user=lambda user_id: True,
        _reply_text=AsyncMock(),
    )
    if not actor_free:
        del runtime._is_authorized_user
    return runtime


def _query(data: str):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=42),
        edit_message_text=AsyncMock(),
        answer=AsyncMock(),
    )


def _update(query):
    return SimpleNamespace(callback_query=query)


def _read(runtime):
    return policy_owner.read_policy(runtime, fallback_menu._actor(runtime))


def _read_for_update(runtime):
    return policy_owner.read_policy(
        runtime, fallback_menu._actor(runtime), for_update=True
    )


def _write(runtime, policy, document):
    return policy_owner.write_policy(
        runtime, fallback_menu._actor(runtime), policy, document=document
    )


def test_fallback_default_card_is_off_and_covers_the_required_facts(tmp_path):
    runtime = _runtime(tmp_path)
    with ui_language.language_scope(None, locale="en"):
        text = fallback_menu_text(runtime)
    assert "FALLBACK" in text.upper()
    assert "OFF" in text
    assert "ordinary general-model requests only" in text
    assert "Privacy:" in text
    assert "Allowed targets, in order:" in text
    assert "No allowed target configured." in text
    assert "Codex CLI" in text
    assert "Extra paid calls" in text
    assert "immediately and persist" in text
    assert "Phone, call, and voice requests are excluded." in text


def test_fallback_command_registration_matches_catalog_description():
    spec = COMMAND_SPEC_BY_NAME["fallback"]
    assert spec.method_name == "cmd_fallback"
    assert spec.guide is not None
    assert spec.guide.usage.startswith("/fallback")
    assert ui_language.command_description(
        "fallback", "fallback", locale="en"
    ) == "Configure the consented model fallback policy"
    assert ui_language.command_description(
        "fallback", "fallback", locale="zh-CN"
    ) == "配置模型备用路由（fallback）策略"


def test_fallback_card_escapes_configuration_values(tmp_path):
    runtime = _runtime(tmp_path)
    policy = policy_owner.FallbackPolicy(
        mode=policy_owner.MODE_ASK,
        routes=(
            policy_owner.FallbackRoute(
                source=policy_owner.SOURCE_HERV3,
                target_kind=policy_owner.TARGET_KIND_HERV3_MODEL,
                provider="openrouter-api",
                model="<script>bad</script>",
                same_model=True,
            ),
        ),
        max_additional_calls=1,
    )
    _saved, document = _read_for_update(runtime)
    _write(runtime, policy, document)
    with ui_language.language_scope(None, locale="en"):
        text = fallback_menu_text(runtime)
    assert "<script>" not in text
    assert "&lt;script&gt;bad&lt;/script&gt;" in text


def test_fallback_locked_target_shows_a_plain_reason(tmp_path):
    runtime = _runtime(
        tmp_path,
        options=[
            {
                "name": "openrouter",
                "engine": "openrouter-api",
                "label": "OpenRouter",
                "models": ["deepseek/deepseek-v4-flash"],
                "available": False,
            }
        ],
    )
    policy = policy_owner.FallbackPolicy(
        mode=policy_owner.MODE_ASK,
        routes=(
            policy_owner.FallbackRoute(
                source=policy_owner.SOURCE_HERV3,
                target_kind=policy_owner.TARGET_KIND_HERV3_MODEL,
                provider="openrouter-api",
                model="deepseek/deepseek-v4-flash",
                same_model=True,
            ),
        ),
    )
    _saved, document = _read_for_update(runtime)
    _write(runtime, policy, document)
    with ui_language.language_scope(None, locale="en"):
        text = fallback_menu_text(runtime)
    assert "🔒" in text
    assert "target key missing" in text


def test_fallback_callbacks_stay_short_and_language_neutral(tmp_path):
    runtime = _runtime(tmp_path)
    markup = fallback_menu_keyboard(runtime)
    payloads = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert payloads
    assert all(len(payload.encode("utf-8")) <= 64 for payload in payloads)
    assert all(payload.isascii() for payload in payloads)


@pytest.mark.asyncio
async def test_fallback_mode_callback_persists_immediately(tmp_path):
    runtime = _runtime(tmp_path)
    query = _query("fallback:mode:auto")
    await callback_fallback(runtime, _update(query), SimpleNamespace())
    saved, _document = _read(runtime)
    assert saved.mode == policy_owner.MODE_AUTO
    assert query.edit_message_text.await_count == 1
    assert "AUTO" in query.edit_message_text.await_args.args[0]


@pytest.mark.asyncio
async def test_fallback_budget_and_unknown_cost_callbacks(tmp_path):
    runtime = _runtime(tmp_path)
    await callback_fallback(
        runtime, _update(_query("fallback:budget:inc")), SimpleNamespace()
    )
    saved, _document = _read(runtime)
    assert saved.max_additional_calls == 2
    await callback_fallback(
        runtime, _update(_query("fallback:budget:unknown")), SimpleNamespace()
    )
    saved, _document = _read(runtime)
    assert saved.allow_unknown_cost is True
    await callback_fallback(
        runtime, _update(_query("fallback:cost:25")), SimpleNamespace()
    )
    saved, _document = _read(runtime)
    assert saved.max_extra_cost_usd == 0.25


@pytest.mark.asyncio
async def test_fallback_add_and_remove_exact_same_model_target(tmp_path):
    runtime = _runtime(tmp_path)
    query = _query("fallback:targets")
    await callback_fallback(runtime, _update(query), SimpleNamespace())
    keyboard = query.edit_message_text.await_args.kwargs["reply_markup"]
    add_buttons = [
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data.startswith("fallback:add:model:herv3:")
    ]
    assert len(add_buttons) == 1
    query2 = _query(add_buttons[0].callback_data)
    await callback_fallback(runtime, _update(query2), SimpleNamespace())
    saved, _document = _read(runtime)
    assert [route.key() for route in saved.routes] == [
        "herv3|herv3_model|openrouter-api|deepseek/deepseek-v4-flash"
    ]
    assert saved.routes[0].same_model is True

    keyboard = query2.edit_message_text.await_args.kwargs["reply_markup"]
    remove_buttons = [
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data.startswith("fallback:remove:")
    ]
    assert len(remove_buttons) == 1
    await callback_fallback(
        runtime, _update(_query(remove_buttons[0].callback_data)), SimpleNamespace()
    )
    saved, _document = _read(runtime)
    assert saved.routes == ()


@pytest.mark.asyncio
async def test_fallback_stale_callback_is_rejected(tmp_path):
    runtime = _runtime(tmp_path)
    query = _query("fallback:remove:0:deadbeef")
    await callback_fallback(runtime, _update(query), SimpleNamespace())
    assert query.edit_message_text.await_count == 0
    assert query.answer.await_count == 1
    assert query.answer.await_args.kwargs.get("show_alert") is True
    with ui_language.language_scope(None, locale="en"):
        expected = ui_language.tr("fallback.error.stale")
    assert query.answer.await_args.args[0] == expected


@pytest.mark.asyncio
async def test_fallback_save_conflict_keeps_the_previous_policy(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)
    policy = policy_owner.FallbackPolicy(mode=policy_owner.MODE_OFF)
    _saved, document = _read_for_update(runtime)
    _write(runtime, policy, document)

    def _conflict(*args, **kwargs):
        raise ConfigConflictError("stale")

    monkeypatch.setattr(policy_owner, "write_policy", _conflict)
    query = _query("fallback:mode:auto")
    await callback_fallback(runtime, _update(query), SimpleNamespace())
    saved, _document = _read(runtime)
    assert saved.mode == policy_owner.MODE_OFF
    assert query.edit_message_text.await_count == 1
    assert "OFF" in query.edit_message_text.await_args.args[0]
    assert query.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_fallback_source_and_privacy_callbacks_stay_independent(tmp_path):
    runtime = _runtime(tmp_path)
    await callback_fallback(
        runtime,
        _update(_query("fallback:source:codex-cli")),
        SimpleNamespace(),
    )
    saved, _document = _read(runtime)
    assert saved.source_rule(policy_owner.SOURCE_CODEX_CLI).enabled is False
    assert saved.source_rule(policy_owner.SOURCE_HERV3).enabled is True

    await callback_fallback(
        runtime,
        _update(_query("fallback:privacy:block")),
        SimpleNamespace(),
    )
    saved, _document = _read(runtime)
    assert saved.privacy_narrowing.get("provider_trust", {}).get("blocked") is True


def test_fallback_submenus_expose_independent_directions_and_short_callbacks(
    tmp_path,
):
    runtime = _runtime(tmp_path)
    markups = (
        fallback_menu._sources_keyboard(runtime),
        fallback_menu._budget_keyboard(runtime),
        fallback_menu._targets_keyboard(runtime),
    )
    payloads = [
        button.callback_data
        for markup in markups
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert {
        "fallback:sourcekind:herv3:herv3",
        "fallback:sourcekind:herv3:codex",
        "fallback:sourcekind:codex-cli:herv3",
        "fallback:sourcekind:codex-cli:codex",
    }.issubset(set(payloads))
    assert all(len(payload.encode("utf-8")) <= 64 for payload in payloads)
    assert all(payload.isascii() for payload in payloads)


@pytest.mark.asyncio
async def test_fallback_codex_source_target_and_route_order_persist(tmp_path):
    runtime = _runtime(tmp_path)
    query = _query("fallback:targets")
    await callback_fallback(runtime, _update(query), SimpleNamespace())
    keyboard = query.edit_message_text.await_args.kwargs["reply_markup"]
    codex_source_buttons = [
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data.startswith("fallback:add:model:codex-cli:")
    ]
    assert codex_source_buttons

    added_codex = _query(codex_source_buttons[0].callback_data)
    await callback_fallback(runtime, _update(added_codex), SimpleNamespace())
    keyboard = added_codex.edit_message_text.await_args.kwargs["reply_markup"]
    herv3_source_button = next(
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data.startswith("fallback:add:model:herv3:")
    )
    added_herv3 = _query(herv3_source_button.callback_data)
    await callback_fallback(runtime, _update(added_herv3), SimpleNamespace())

    saved, _document = _read(runtime)
    assert [route.source for route in saved.routes] == [
        policy_owner.SOURCE_CODEX_CLI,
        policy_owner.SOURCE_HERV3,
    ]
    assert saved.routes[0].same_model is False
    keyboard = added_herv3.edit_message_text.await_args.kwargs["reply_markup"]
    move_up = next(
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data.startswith("fallback:move:1:up:")
    )
    await callback_fallback(
        runtime, _update(_query(move_up.callback_data)), SimpleNamespace()
    )
    saved, _document = _read(runtime)
    assert [route.source for route in saved.routes] == [
        policy_owner.SOURCE_HERV3,
        policy_owner.SOURCE_CODEX_CLI,
    ]


@pytest.mark.asyncio
async def test_fallback_ask_confirmation_is_bound_and_one_time(
    tmp_path, monkeypatch
):
    from orchestrator import runtime_delivery

    runtime = _runtime(tmp_path)
    runtime.current_request_meta = {"request_id": "req-ask", "chat_id": 42}
    send = AsyncMock(return_value=(0.0, ["sent"]))
    monkeypatch.setattr(runtime_delivery, "send_long_message", send)
    confirmation = policy_owner.FallbackConfirmation(
        action_id="ask-once",
        request_id="req-ask",
        route_key="herv3|codex_cli|codex-cli|",
        privacy_level=1,
        budget_used_at_issue=0,
        issued_at=time.time(),
        expires_at=time.time() + 5.0,
    )
    waiter = asyncio.create_task(
        fallback_menu.request_fallback_confirmation(
            runtime,
            confirmation,
            target_label="Codex CLI",
            request_id="req-ask",
        )
    )
    await asyncio.sleep(0)
    query = _query("fallback:confirm:ask-once:allow")
    await callback_fallback(runtime, _update(query), SimpleNamespace())
    assert await waiter == confirmation
    assert send.await_args.kwargs["reply_markup"] is not None

    stale = _query("fallback:confirm:ask-once:allow")
    await callback_fallback(runtime, _update(stale), SimpleNamespace())
    assert stale.answer.await_args.kwargs.get("show_alert") is True
