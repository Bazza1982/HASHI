from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.commands import restart_lifeline
from orchestrator.restart_provider import RestartProviderError


class _Runtime:
    def __init__(self) -> None:
        self.name = "hashiko"
        self.global_config = SimpleNamespace(
            instance_id="HASHI3",
            bridge_home=Path("C:/hashi3-runtime"),
        )
        self.messages: list[str] = []

    def _is_authorized_user(self, user_id):
        return user_id == 1

    async def _reply_text(self, update, text, **kwargs):
        self.messages.append(text)


def _update():
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=777),
    )


@pytest.mark.asyncio
async def test_restart_defaults_to_own_instance_remote(monkeypatch, tmp_path):
    runtime = _Runtime()
    runtime.global_config.bridge_home = tmp_path / "hashi3-runtime"
    observed = {}
    provider = {
        "kind": "local_remote",
        "source_instance": "HASHI3",
        "target_instance": "HASHI3",
        "provider_instance": "HASHI3",
    }

    def fake_local_provider(*, instance_id, hashi_root):
        observed["provider_instance_id"] = instance_id
        observed["provider_hashi_root"] = hashi_root
        return provider

    monkeypatch.setattr(restart_lifeline, "local_restart_provider", fake_local_provider)
    monkeypatch.setattr(
        restart_lifeline,
        "peer_restart_provider",
        lambda target: pytest.fail(f"unexpected peer provider: {target}"),
    )

    def fake_admission(provider_arg, **kwargs):
        observed.update(provider=provider_arg, kwargs=kwargs)
        origin = kwargs["notify_origin"]
        return 0, {"restart_id": "rst_20261111T060000Z_abcdef123456",
            "state": "accepted", "target_instance": "HASHI3", "controlled_instance": "HASHI3",
            "requester_agent": runtime.name, "notify_origin": origin}
    monkeypatch.setattr(restart_lifeline, "restart_via_provider", fake_admission)
    result = await restart_lifeline.restart_command(runtime, _update(),
        SimpleNamespace(args=[], source_channel="telegram"))
    assert result["accepted"] is True
    assert result["operation"]["terminal"] is False
    assert result["operation"]["operation_id"] == "rst_20261111T060000Z_abcdef123456"
    assert observed["provider"] is provider
    assert observed["provider_instance_id"] == "HASHI3"
    assert observed["provider_hashi_root"] == runtime.global_config.bridge_home
    assert observed["kwargs"]["request_source"] == "telegram"
    assert observed["kwargs"]["detached"] is True
    assert observed["kwargs"]["notify_origin"]["agent_id"] == runtime.name
    assert runtime._remote_restart_inflight is False

    assert len(runtime.messages) == 1
    assert runtime.messages[0].strip()


@pytest.mark.asyncio
async def test_restart_does_not_fall_back_to_watchtower(monkeypatch):
    runtime = _Runtime()

    def unavailable():
        raise RestartProviderError("L3_RESTART is not enabled")

    monkeypatch.setattr(restart_lifeline, "local_restart_provider", unavailable)

    await restart_lifeline.restart_command(
        runtime,
        _update(),
        SimpleNamespace(args=[], source_channel="telegram"),
    )

    assert runtime.messages == [
        "This instance's HASHI Remote or fixed restart provider is unavailable. Check the instance installation."
    ]
# HASHI-Fingerprint: hfp_f1c04a552a1443248ad35cc81e60f571
