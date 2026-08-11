from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from adapters.base import BaseBackend, BackendCapabilities, BackendResponse
from adapters.timeout_policy import HARD_TIMEOUT_KEY, IDLE_TIMEOUT_KEY, apply_timeout_layers
from orchestrator import runtime_timeout


class _Backend(BaseBackend):
    DEFAULT_IDLE_TIMEOUT_SEC = 3600
    DEFAULT_HARD_TIMEOUT_SEC = 86400

    def _define_capabilities(self):
        return BackendCapabilities(False, False, False, False, True)

    async def initialize(self):
        return True

    async def generate_response(
        self,
        prompt,
        request_id,
        is_retry=False,
        silent=False,
        on_stream_event=None,
    ):
        return BackendResponse(text=prompt, duration_ms=0)

    async def shutdown(self):
        return None

    async def handle_new_session(self):
        return True


class _Message:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


def _runtime(tmp_path):
    configured = {
        IDLE_TIMEOUT_KEY: 120,
        HARD_TIMEOUT_KEY: 1200,
    }
    extra = apply_timeout_layers(
        configured,
        engine="codex-cli",
        agent_extra=configured,
    )
    config = SimpleNamespace(
        name="agent",
        engine="codex-cli",
        workspace_dir=tmp_path,
        extra=extra,
    )
    backend = _Backend(config, SimpleNamespace())
    return SimpleNamespace(name="agent", backend=backend)


def _update():
    message = _Message()
    return SimpleNamespace(message=message), message


@pytest.mark.asyncio
async def test_timeout_command_persists_until_reset_and_restores_configuration(tmp_path):
    runtime = _runtime(tmp_path)
    update, message = _update()

    await runtime_timeout.cmd_timeout(
        runtime,
        update,
        SimpleNamespace(args=["60", "6000"]),
    )

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["backend_timeouts"]["codex-cli"] == {
        IDLE_TIMEOUT_KEY: 3600,
        HARD_TIMEOUT_KEY: 360000,
    }
    assert runtime.backend.IDLE_TIMEOUT_SEC == 3600
    assert runtime.backend.HARD_TIMEOUT_SEC == 360000
    assert "remains active until /timeout reset" in message.replies[-1][0]

    await runtime_timeout.cmd_timeout(
        runtime,
        update,
        SimpleNamespace(args=[]),
    )
    menu, kwargs = message.replies[-1]
    assert kwargs["parse_mode"] == "HTML"
    assert "user override" in menu
    assert "codex-cli" in menu

    await runtime_timeout.cmd_timeout(
        runtime,
        update,
        SimpleNamespace(args=["reset"]),
    )

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "backend_timeouts" not in state
    assert runtime.backend.IDLE_TIMEOUT_SEC == 120
    assert runtime.backend.HARD_TIMEOUT_SEC == 1200
    assert "agent configuration" in message.replies[-1][0]


@pytest.mark.asyncio
async def test_timeout_command_rejects_hard_limit_below_idle_limit(tmp_path):
    runtime = _runtime(tmp_path)
    update, message = _update()

    await runtime_timeout.cmd_timeout(
        runtime,
        update,
        SimpleNamespace(args=["60", "30"]),
    )

    assert "greater than or equal" in message.replies[-1][0]
    assert not (tmp_path / "state.json").exists()
    assert runtime.backend.IDLE_TIMEOUT_SEC == 120
    assert runtime.backend.HARD_TIMEOUT_SEC == 1200
