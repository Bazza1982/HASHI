from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.frontend_delivery import (
    delivery_intent_from_run_route,
    freeze_run_delivery_route,
    normalize_tui_run_delivery_policy,
    project_run_delivery_route,
    route_destination,
    telegram_delivery_for_admission,
    tui_request_metadata,
    tui_run_delivery_policy,
)
from orchestrator.frontend_status import runtime_presentation_status


@pytest.mark.asyncio
async def test_native_telegram_text_message_enters_admission_as_telegram(
    tmp_path, monkeypatch
):
    from orchestrator import flexible_agent_runtime as runtime_module
    from orchestrator import runtime_long, runtime_scheduler_recovery, runtime_workzone
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    accepted = {}

    class _TelegramRuntime:
        name = "testing"
        global_config = SimpleNamespace(authorized_id=42, bridge_home=tmp_path)

        def _is_authorized_user(self, user_id):
            return user_id == 42

        def _record_active_chat(self, _update):
            return None

        def _should_redirect_after_transfer(self):
            return False

        async def enqueue_request(
            self, chat_id, prompt, source, summary, *, reply_to_message_id=None
        ):
            accepted.update(
                chat_id=chat_id,
                prompt=prompt,
                source=source,
                summary=summary,
                reply_to_message_id=reply_to_message_id,
                telegram_requested=telegram_delivery_for_admission(
                    source=source,
                    request_metadata=None,
                    state_root=tmp_path,
                ),
            )
            return "req-testing"

    async def allow_channel(_runtime, _update, *, source_channel):
        return source_channel == "telegram"

    async def no_pending_path(_runtime, _update):
        return False

    async def forbidden_recovery_interceptor(_runtime, *, text, chat_id):
        raise AssertionError(
            "ordinary Telegram text must not be intercepted before FC/PAO admission"
        )

    monkeypatch.setattr(
        FlexibleAgentRuntime, "_telegram_channel_allowed", allow_channel
    )
    monkeypatch.setattr(runtime_workzone, "handle_pending_path_reply", no_pending_path)
    monkeypatch.setattr(runtime_long, "collect_text", lambda *_args: False)
    monkeypatch.setattr(
        runtime_scheduler_recovery,
        "handle_reply",
        forbidden_recovery_interceptor,
        raising=False,
    )
    monkeypatch.setattr(runtime_module, "_print_user_message", lambda *_args: None)

    message = SimpleNamespace(text="hello from Telegram")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=99),
        message=message,
    )

    await FlexibleAgentRuntime.handle_message(_TelegramRuntime(), update, None)

    assert accepted["source"] == "telegram"
    assert accepted["telegram_requested"] is True
    assert accepted["chat_id"] == 99
    assert accepted["prompt"] == "hello from Telegram"
    assert accepted["reply_to_message_id"] is None


def test_frontend_delivery_policy_is_connector_neutral_client_bound_and_fail_visible():
    policy = tui_run_delivery_policy(
        telegram_mirror=False,
        client_id="tui-window-a",
    )
    metadata = tui_request_metadata(
        telegram_mirror=False,
        client_id="tui-window-a",
    )

    assert normalize_tui_run_delivery_policy(
        policy,
        client_id="tui-window-a",
    ) == policy
    assert policy["type"] == "hashi.frontend-delivery-policy"
    assert policy["version"] == 2
    assert policy["connector_id"] == "tui"
    assert policy["targets"] == [
        {"connector_id": "telegram", "role": "mirror", "enabled": False}
    ]
    assert telegram_delivery_for_admission(
        source="tui",
        request_metadata=metadata,
    ) is True
    assert telegram_delivery_for_admission(
        source="api",
        request_metadata=metadata,
    ) is True
    assert telegram_delivery_for_admission(
        source="telegram",
        request_metadata=metadata,
    ) is True
    assert telegram_delivery_for_admission(
        source="tui",
        request_metadata=tui_request_metadata(
            telegram_mirror=True,
            client_id="tui-window-a",
        ),
    ) is True
    forged = dict(metadata)
    forged["frontend_client"] = {"kind": "tui", "client_id": "other-window"}
    assert telegram_delivery_for_admission(
        source="tui",
        request_metadata=forged,
    ) is True
    assert telegram_delivery_for_admission(
        source="tui",
        request_metadata=None,
    ) is True


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(version=1),
        lambda value: value.update(scope="session"),
        lambda value: value.update(connector_id="browser"),
        lambda value: value.update(targets=[{"connector_id": "telegram", "role": "mirror", "enabled": "off"}]),
    ],
)
def test_invalid_frontend_delivery_policy_is_rejected(mutation):
    policy = tui_run_delivery_policy(
        telegram_mirror=False,
        client_id="tui-window-a",
    )
    mutation(policy)
    with pytest.raises(ValueError):
        normalize_tui_run_delivery_policy(policy, client_id="tui-window-a")


def test_legacy_tui_delivery_policy_is_normalized_to_generic_contract():
    legacy = {
        "type": "hashi.frontend-delivery",
        "version": 1,
        "scope": "run",
        "frontend": "tui",
        "client_id": "tui-window-a",
        "telegram": {"mirror": False},
    }
    normalized = normalize_tui_run_delivery_policy(
        legacy, client_id="tui-window-a"
    )
    assert normalized["type"] == "hashi.frontend-delivery-policy"
    assert normalized["targets"][0]["enabled"] is False


@pytest.mark.parametrize(
    ("source_id", "session_surface", "telegram", "primary", "mirrors"),
    [
        ("telegram", "telegram", True, "telegram", []),
        ("tui", "workbench", False, "tui", []),
        ("tui", "workbench", True, "tui", ["telegram"]),
        ("api", "workbench", True, "workbench", ["telegram"]),
        ("hchat", "workbench", True, "hchat", ["telegram"]),
        ("whatsapp", "whatsapp", True, "whatsapp", ["telegram"]),
        ("hashi.internal", "scheduled", True, "telegram", []),
    ],
)
def test_pao_freezes_one_cross_connector_run_route(
    source_id, session_surface, telegram, primary, mirrors
):
    route = freeze_run_delivery_route(
        message_source_id=source_id,
        session_surface=session_surface,
        session_channel_key="channel-a",
        chat_id=123,
        telegram_requested=telegram,
        primary_channel_key=("peer@HASHI2" if source_id == "hchat" else None),
    )

    assert project_run_delivery_route(route) == {
        "surface": primary,
        "mirrors": mirrors,
        "automatic": True,
        "telegram_mirror": "telegram" in mirrors,
    }
    assert (route_destination(route, "telegram") is not None) is (
        primary == "telegram" or "telegram" in mirrors
    )
    if source_id == "hchat":
        assert route["primary"]["channel_key"] == "peer@HASHI2"


def test_terminal_hchat_reply_routes_to_user_without_acknowledgement_loop():
    route = freeze_run_delivery_route(
        message_source_id="hchat",
        session_surface="workbench",
        session_channel_key="default",
        primary_channel_key="peer@HASHI2",
        chat_id=123,
        telegram_requested=True,
        terminal_exchange=True,
    )

    assert project_run_delivery_route(route) == {
        "surface": "telegram",
        "mirrors": [],
        "automatic": True,
        "telegram_mirror": False,
    }
    assert route_destination(route, "hchat") is None


def test_legacy_run_route_projects_to_endpoint_level_delivery_intent():
    route = freeze_run_delivery_route(
        message_source_id="api",
        session_surface="workbench",
        session_channel_key="workbench:primary",
        chat_id=123,
        telegram_requested=True,
    )
    intent = delivery_intent_from_run_route(
        route,
        event_id="evt-1",
        session_id="ses_1",
        idempotency_key="meter:req-1",
        content_modes=["text", "media"],
    )
    assert intent["version"] == 2
    assert [
        (item["connector_id"], item["role"])
        for item in intent["destinations"]
    ] == [("backend_api", "primary"), ("telegram", "mirror")]


def test_internal_run_without_a_connector_target_is_explicitly_nonautomatic():
    route = freeze_run_delivery_route(
        message_source_id="hashi.internal",
        session_surface="scheduled",
        session_channel_key="default",
        chat_id=None,
        telegram_requested=False,
    )

    assert project_run_delivery_route(route) == {
        "surface": "none",
        "mirrors": [],
        "automatic": False,
        "telegram_mirror": False,
    }


def test_runtime_presentation_status_reports_her_v3_main_model():
    main = SimpleNamespace(engine="anthropic", model="claude-pro")
    runtime = SimpleNamespace(
        config=SimpleNamespace(active_backend="her-v2"),
        backend_manager=SimpleNamespace(
            current_backend=SimpleNamespace(
                _v2_config=SimpleNamespace(profiles={"main": main})
            )
        ),
        get_current_model=lambda: "claude-pro",
        _get_current_effort=lambda: "high",
        _think=True,
        _verbose=False,
        _commentary=True,
    )

    status = runtime_presentation_status(runtime)

    assert status["engine"] == "her-v3"
    assert status["her_v3"] == {"main": {"provider": "anthropic", "model": "claude-pro"}}
    assert "her_v2" not in status
    assert status["effort"] == "high"


def test_runtime_presentation_status_omits_provider_for_other_engines():
    runtime = SimpleNamespace(
        config=SimpleNamespace(active_backend="codex-cli"),
        get_current_model=lambda: "gpt-codex",
        _get_current_effort=lambda: "high",
        _think=False,
        _verbose=True,
        _commentary=False,
    )

    status = runtime_presentation_status(runtime)

    assert status["engine"] == "codex-cli"
    assert status["model"] == "gpt-codex"
    assert "provider" not in status
    assert "her_v2" not in status
