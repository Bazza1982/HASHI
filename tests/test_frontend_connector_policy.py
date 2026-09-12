from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.frontend_delivery import (
    freeze_run_delivery_route,
    normalize_tui_run_delivery_policy,
    project_run_delivery_route,
    route_destination,
    telegram_delivery_for_admission,
    tui_request_metadata,
    tui_run_delivery_policy,
)
from orchestrator.frontend_status import runtime_presentation_status


def test_tui_delivery_policy_is_typed_client_bound_and_fail_visible():
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
    assert telegram_delivery_for_admission(
        source="tui",
        request_metadata=metadata,
    ) is False
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
        lambda value: value.update(version=2),
        lambda value: value.update(scope="session"),
        lambda value: value.update(frontend="browser"),
        lambda value: value.update(telegram={"mirror": "off"}),
    ],
)
def test_invalid_tui_delivery_policy_is_rejected(mutation):
    policy = tui_run_delivery_policy(
        telegram_mirror=False,
        client_id="tui-window-a",
    )
    mutation(policy)
    with pytest.raises(ValueError):
        normalize_tui_run_delivery_policy(policy, client_id="tui-window-a")


@pytest.mark.parametrize(
    ("source_id", "session_surface", "telegram", "primary", "mirrors"),
    [
        ("telegram", "telegram", True, "telegram", []),
        ("tui", "workbench", False, "tui", []),
        ("tui", "workbench", True, "tui", ["telegram"]),
        ("api", "workbench", True, "workbench", ["telegram"]),
        ("hchat", "workbench", True, "hchat", ["telegram"]),
        ("whatsapp", "whatsapp", True, "whatsapp", []),
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


def test_runtime_presentation_status_reports_structured_her_quick_and_pro():
    selected = SimpleNamespace(
        routing_mode="hybrid",
        routing_revision=7,
        target_for_slot=lambda slot: (
            SimpleNamespace(provider="openai", model="gpt-quick")
            if slot == "quick"
            else SimpleNamespace(provider="anthropic", model="claude-pro")
        ),
    )
    runtime = SimpleNamespace(
        config=SimpleNamespace(active_backend="her-v2"),
        backend_manager=SimpleNamespace(get_her_v2_configuration=lambda: selected),
        get_current_model=lambda: "mixed",
        _get_current_effort=lambda: "planned",
        _think=True,
        _verbose=False,
        _commentary=True,
    )

    status = runtime_presentation_status(runtime)

    assert status["engine"] == "her-v2"
    assert status["her_v2"] == {
        "routing_mode": "hybrid",
        "quick": {"provider": "openai", "model": "gpt-quick"},
        "pro": {"provider": "anthropic", "model": "claude-pro"},
        "routing_revision": 7,
    }
    assert status["effort"] == "planned"


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
