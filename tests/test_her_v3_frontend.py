"""Observable HER v3 command presentation while the storage ID remains her-v2."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.runtime_menu_views import her_commentary_text
from orchestrator.runtime_model_selection import callback_model, cmd_model, cmd_provider


def _runtime():
    main = SimpleNamespace(engine="deepseek-api", model="deepseek-v4-pro")
    legacy = SimpleNamespace(
        routing_mode="single",
        provider="deepseek-api",
        fast_provider="deepseek-api",
        fast_model="old-fast",
        pro_provider="deepseek-api",
        pro_model="old-pro",
    )
    manager = SimpleNamespace(
        agent_mode="flex",
        current_backend=SimpleNamespace(
            _v2_config=SimpleNamespace(profiles={"main": main})
        ),
        get_her_v2_edit_configuration=lambda: legacy,
        has_her_v2_configuration_draft=lambda: False,
    )
    runtime = SimpleNamespace(
        config=SimpleNamespace(active_backend="her-v2"),
        backend_manager=manager,
        _is_authorized_user=lambda _id: True,
        _reply_text=AsyncMock(),
        _get_available_efforts=lambda: [
            "none", "low", "medium", "high", "xhigh", "max"
        ],
        _get_current_effort=lambda: "high",
    )
    runtime.get_current_provider = lambda: FlexibleAgentRuntime.get_current_provider(runtime)
    runtime.get_current_model = lambda: FlexibleAgentRuntime.get_current_model(runtime)
    return runtime


@pytest.mark.asyncio
async def test_model_menu_reports_the_actual_single_main_model():
    runtime = _runtime()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    await cmd_model(runtime, update, SimpleNamespace(args=[]))

    text = runtime._reply_text.await_args.args[1]
    assert "HER V3" in text
    assert "deepseek-v4-pro" in text
    assert "old-fast" not in text
    assert "old-pro" not in text
    assert runtime._reply_text.await_args.kwargs["reply_markup"] is None


def test_effort_menu_reports_model_reasoning_not_old_execution_modes():
    runtime = _runtime()

    text = FlexibleAgentRuntime._build_effort_followup_text(runtime)

    assert "HER v3" in text
    assert "deepseek-v4-pro" in text
    assert "old-fast" not in text
    assert "old-pro" not in text
    assert "execution mode" not in text.lower()


@pytest.mark.asyncio
async def test_old_route_callback_cannot_reopen_v2_editor():
    runtime = _runtime()
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        data="her_execution",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )

    await callback_model(runtime, SimpleNamespace(callback_query=query), None)

    assert "HER v3" in query.answer.await_args.args[0]
    query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_menu_does_not_offer_v2_quick_pro_routes():
    runtime = _runtime()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    await cmd_provider(runtime, update, SimpleNamespace(args=[]))

    text = runtime._reply_text.await_args.args[1]
    assert "HER V3" in text
    assert "deepseek-v4-pro" in text
    assert "old-fast" not in text
    assert "old-pro" not in text
    assert runtime._reply_text.await_args.kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_legacy_reasoning_stages_callback_cannot_reopen_v2_editor():
    runtime = _runtime()
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        data="her_reasoning_stages",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )

    await callback_model(runtime, SimpleNamespace(callback_query=query), None)

    assert "HER v3" in query.answer.await_args.args[0]
    query.edit_message_text.assert_not_awaited()


def test_commentary_menu_does_not_describe_retired_effort_stages():
    text = her_commentary_text(enabled=True, effort="high")

    assert "HER v3" in text
    assert "Direct" not in text
    assert "Planned" not in text
    assert "Adaptive" not in text
