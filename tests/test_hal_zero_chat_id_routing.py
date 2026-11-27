"""Regression: HAL prompts must reach Workbench-only Agents.

HAL used to pass ``deliver_to_telegram=True`` and the runtime's primary
chat-id lookup (``0`` when the Agent has no Telegram route) into admission.
Session routing read ``0`` as a Telegram channel key ``"0"``, the Telegram
route record rejected it and the prompt failed with SessionConflict before it
could execute.  Zero and blank chat ids mean "no channel"; the request belongs
to the shared Workbench Session.
"""
from __future__ import annotations

import asyncio
from types import MethodType, SimpleNamespace

from hal.dispatch import GatewayDispatchAdapter
from orchestrator import runtime_session
from orchestrator.session_store import SessionStore


class _RecordingRuntime:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def enqueue_api_text(self, text, **kwargs):
        self.calls.append({"text": text, **kwargs})
        return {"status": "accepted"}


class _Server:
    def __init__(self, runtime) -> None:
        self.runtime = runtime

    def _runtime_map(self):
        return {"wren": self.runtime}


def _workbench_only_runtime(tmp_path) -> SimpleNamespace:
    store = SessionStore(
        tmp_path / "state" / "sessions.sqlite3", instance_id="HASHI1"
    )
    return SimpleNamespace(
        name="wren",
        session_store=store,
        global_config=SimpleNamespace(instance_id="HASHI1", owner_scope_id="user:7"),
        config=SimpleNamespace(active_backend="her-v2"),
    )


def test_hal_with_zero_chat_id_resolves_to_the_shared_workbench_session(tmp_path):
    runtime = _workbench_only_runtime(tmp_path)

    session, owner, surface, channel = runtime_session.resolve_request_session(
        runtime, source="hal", chat_id=0, metadata={}
    )

    assert surface == "workbench"
    assert channel == "default"
    assert owner == "user:7"
    assert session["agent_id"] == "wren"


def test_zero_and_blank_chat_ids_are_not_telegram_channels():
    for value in (0, "0", "", "   ", None):
        surface, channel, _ = runtime_session._surface_and_channel(
            None, source="hal", chat_id=value
        )
        assert (surface, channel) == ("workbench", "default"), value

    surface, channel, _ = runtime_session._surface_and_channel(
        None, source="hal", chat_id=12345
    )
    assert (surface, channel) == ("telegram", "12345")


def test_hal_prompt_does_not_force_telegram_for_a_workbench_only_agent(monkeypatch):
    runtime = _RecordingRuntime()
    monkeypatch.setattr(
        runtime_session, "primary_telegram_chat_id", lambda _runtime: None
    )

    result = asyncio.run(
        GatewayDispatchAdapter(_Server(runtime)).dispatch_prompt(
            target_agent="wren",
            text="daily check",
            idempotency_key="hal:exec:1",
            exec_id="exec-1",
            stage=0,
        )
    )

    assert result.accepted is True
    assert runtime.calls[0]["chat_id"] == 0
    assert runtime.calls[0]["deliver_to_telegram"] is False


def test_hal_prompt_uses_workbench_when_the_agents_old_telegram_binding_is_offline(monkeypatch):
    runtime = _RecordingRuntime()
    runtime.telegram_connected = False
    monkeypatch.setattr(runtime_session, "primary_telegram_chat_id", lambda _runtime: 424242)

    result = asyncio.run(GatewayDispatchAdapter(_Server(runtime)).dispatch_prompt(
        target_agent="wren", text="daily check", idempotency_key="hal:exec:3",
        exec_id="exec-3", stage=0,
    ))

    assert result.accepted is True
    assert runtime.calls[0]["chat_id"] == 0
    assert runtime.calls[0]["deliver_to_telegram"] is False


def test_hal_workbench_route_survives_the_workers_legacy_chat_id_default(tmp_path, monkeypatch):
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    runtime = _workbench_only_runtime(tmp_path)
    runtime.telegram_connected = False
    runtime._should_redirect_after_transfer = lambda: False
    runtime._primary_chat_id = lambda: 424242
    runtime.enqueue_api_text = MethodType(FlexibleAgentRuntime.enqueue_api_text, runtime)
    admitted_routes = []

    async def enqueue_request(chat_id, _text, source, _summary, **kwargs):
        _, _, surface, channel = runtime_session.resolve_request_session(
            runtime, source=source, chat_id=chat_id, metadata=kwargs["request_metadata"],
        )
        admitted_routes.append((surface, channel, kwargs["deliver_to_telegram"]))
        return "request-test"

    runtime.enqueue_request = enqueue_request
    monkeypatch.setattr(runtime_session, "primary_telegram_chat_id", lambda _runtime: 424242)
    result = asyncio.run(GatewayDispatchAdapter(_Server(runtime)).dispatch_prompt(
        target_agent="wren", text="daily check", idempotency_key="hal:exec:4",
        exec_id="exec-4", stage=0,
    ))

    assert result.accepted is True
    assert admitted_routes == [("workbench", "default", False)]


def test_hal_prompt_keeps_the_agents_own_telegram_route(monkeypatch):
    runtime = _RecordingRuntime()
    runtime.telegram_connected = True
    monkeypatch.setattr(
        runtime_session, "primary_telegram_chat_id", lambda _runtime: 424242
    )

    asyncio.run(
        GatewayDispatchAdapter(_Server(runtime)).dispatch_prompt(
            target_agent="wren",
            text="daily check",
            idempotency_key="hal:exec:2",
            exec_id="exec-2",
            stage=0,
        )
    )

    assert runtime.calls[0]["chat_id"] == 424242
    assert runtime.calls[0]["deliver_to_telegram"] is True
# HASHI-Fingerprint: hfp_c3c29abc5df2443fa1792fe27bd84580
