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
        "reference",
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


@pytest.mark.asyncio
async def test_logo_command_remains_available_to_tui(tmp_path, monkeypatch):
    from orchestrator import runtime_display
    from orchestrator.admin_local_testing import execute_local_command
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    animations = []

    async def reply_text(update, text, **kwargs):
        return await update.message.reply_text(text, **kwargs)

    runtime = SimpleNamespace(
        name="testing",
        workspace_dir=tmp_path,
        global_config=SimpleNamespace(authorized_id=42),
        _is_authorized_user=lambda user_id: user_id == 42,
        _reply_text=reply_text,
    )
    runtime.cmd_logo = types.MethodType(FlexibleAgentRuntime.cmd_logo, runtime)
    monkeypatch.setattr(runtime_display, "_show_logo_animation", lambda: animations.append(True))

    result = await execute_local_command(runtime, "/logo", source_channel="tui")

    assert result["ok"] is True
    assert animations == [True]


@pytest.mark.parametrize("override", [False, True])
def test_new_connector_cannot_enable_terminal_logo(tmp_path, override):
    from orchestrator.frontend_connector_registry import (
        connector_registry_snapshot,
        get_connector_capabilities,
        register_connector,
        unregister_connector,
    )
    from orchestrator.frontend_contracts import build_frontend_ingress_envelope
    from orchestrator.frontend_ingress import admit_frontend_ingress
    from orchestrator.session_store import SessionStore

    connector_id = "third_party_terminal_test"
    register_connector({
        "id": connector_id,
        "class": "external_client",
        "ingress": ["message", "command"],
        "egress": ["text"],
        "customizations": [{
            "kind": "command_override", "key": "logo", "route": "standard",
            "semantic": "terminal_logo_display", "reason": "local_presentation",
        }] if override else [],
    })
    try:
        store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI1")
        session = store.ensure_default_session(owner_id="user:42", agent_id="testing")
        executions = []
        runtime = SimpleNamespace(
            name="testing",
            global_config=SimpleNamespace(instance_id="HASHI1", authorized_id=42),
            session_store=store,
            command_registry=SimpleNamespace(
                has_command=lambda name: name == "logo",
                get_command=lambda name: SimpleNamespace(handler=lambda rt: executions.append(name)),
            ),
        )
        envelope = build_frontend_ingress_envelope(
            source_id=connector_id, ingress_transport=connector_id, surface=connector_id,
            channel_key="default", instance_id="HASHI1",
            principal={"kind": "human_or_client", "assurance": "runtime_observed"},
            network_authentication="not_applicable", relay_chain=[],
            request_id="req-terminal-logo", idempotency_key="key-terminal-logo",
            session_id=session["session_id"], agent_id="testing",
        )
        receipt = admit_frontend_ingress(runtime, envelope, command_name="logo")
        assert receipt["status"] == "rejected"
        assert executions == []
        with pytest.raises(ConnectorLocalCommand):
            normalize_compatibility_command(
                "/logo", source_channel=connector_id,
                session_metadata={"connector_id": connector_id},
            )

        descriptor = next(item for item in connector_registry_snapshot()["connectors"]
                          if item["id"] == connector_id)
        for projection in (descriptor, get_connector_capabilities(connector_id)):
            rule = next(item for item in projection["customizations"] if item["key"] == "logo")
            assert rule["route"] == "connector_local"
    finally:
        unregister_connector(connector_id)


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
