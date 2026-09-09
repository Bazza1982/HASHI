from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.frontend_delivery import (
    normalize_tui_run_delivery_policy,
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
