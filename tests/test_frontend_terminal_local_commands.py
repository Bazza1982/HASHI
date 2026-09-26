from __future__ import annotations

# ruff: noqa: E402 - dependency stubs must be installed before runtime imports.

import sys
import types
from types import SimpleNamespace

import pytest

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

from orchestrator.command_specs import COMMAND_SPECS
from orchestrator.frontend_compatibility import (
    ConnectorLocalCommand,
    normalize_compatibility_command,
)
from orchestrator.runtime_command_binding import BOT_COMMAND_BINDINGS


@pytest.mark.parametrize(
    "connector_id",
    [
        "telegram",
        "backend_api",
        "session_api",
        "external",
        "hchat",
        "remote",
        "exchange",
        "whatsapp",
    ],
)
def test_logo_command_is_registered_as_local_outside_tui(connector_id):
    spec = next(item for item in COMMAND_SPECS if item.name == "logo")
    assert spec.menu_visible is False
    assert "logo" not in {binding.name for binding in BOT_COMMAND_BINDINGS}
    with pytest.raises(ConnectorLocalCommand):
        normalize_compatibility_command(
            "/logo",
            source_channel=connector_id,
            session_metadata={"connector_id": connector_id},
        )


def test_logo_command_remains_available_to_tui():
    result = normalize_compatibility_command(
        "/logo",
        source_channel="tui",
        session_metadata={"connector_id": "tui"},
    )
    assert result["connector_id"] == "tui"
    assert result["operation"]["name"] == "logo"


@pytest.mark.asyncio
async def test_native_telegram_logo_command_does_not_run_terminal_animation(monkeypatch):
    from orchestrator import runtime_display, ui_language
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    animations = []
    replies = []

    def play_logo():
        animations.append(True)

    async def reply_text(_update, text, **_kwargs):
        replies.append(text)

    runtime = SimpleNamespace(
        name="testing",
        _is_authorized_user=lambda user_id: user_id == 42,
        _reply_text=reply_text,
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=42))
    monkeypatch.setattr(runtime_display, "_show_logo_animation", play_logo)

    await FlexibleAgentRuntime.cmd_logo(runtime, update, SimpleNamespace())

    assert animations == []
    assert replies == [ui_language.tr("runtime.logo_terminal_only")]
