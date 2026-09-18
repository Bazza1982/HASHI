from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator.commands import restart_lifeline
from orchestrator.restart_provider import RestartProviderError


class _Runtime:
    def __init__(self) -> None:
        self.name = "hashiko"
        self.global_config = SimpleNamespace(instance_id="HASHI3")
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
async def test_restart_defaults_to_own_instance_remote(monkeypatch):
    runtime = _Runtime()
    observed = {}
    provider = {
        "kind": "local_remote",
        "source_instance": "HASHI3",
        "target_instance": "HASHI3",
        "provider_instance": "HASHI3",
    }

    monkeypatch.setattr(restart_lifeline, "local_restart_provider", lambda: provider)
    monkeypatch.setattr(
        restart_lifeline,
        "peer_restart_provider",
        lambda target: pytest.fail(f"unexpected peer provider: {target}"),
    )

    async def fake_dispatch(runtime_arg, chat_id, provider_arg, **kwargs):
        observed.update(
            runtime=runtime_arg,
            chat_id=chat_id,
            provider=provider_arg,
            kwargs=kwargs,
        )

    monkeypatch.setattr(restart_lifeline, "_dispatch_remote_restart", fake_dispatch)

    await restart_lifeline.restart_command(
        runtime,
        _update(),
        SimpleNamespace(args=[], source_channel="telegram"),
    )
    await asyncio.sleep(0)

    assert observed["runtime"] is runtime
    assert observed["chat_id"] == 777
    assert observed["provider"] is provider
    assert observed["kwargs"]["request_source"] == "telegram"
    assert runtime.messages == [
        "🔁 Restarting HASHI3. A final result will follow automatically."
    ]


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
        "This instance's HASHI Remote is unavailable or does not have L3_RESTART enabled."
    ]
