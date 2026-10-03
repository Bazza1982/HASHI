from __future__ import annotations

import pytest

from orchestrator.frontend_connector_registry import (
    canonical_connector_id,
    connector_registry_snapshot,
    get_compatibility_adapter,
    get_connector_customization,
    require_connector_event,
    require_connector_operation,
)
from orchestrator.frontend_contracts import (
    build_frontend_ingress_envelope,
    normalize_frontend_event,
    normalize_frontend_request,
)


def _envelope(*, source: str, transport: str, surface: str) -> dict:
    return build_frontend_ingress_envelope(
        source_id=source,
        ingress_transport=transport,
        surface=surface,
        channel_key="private-endpoint",
        instance_id="HASHI1",
        principal={
            "kind": "human_or_client",
            "assurance": "runtime_observed",
        },
        network_authentication="not_applicable",
        relay_chain=[],
        request_id=f"req-{surface}",
        idempotency_key=f"key-{surface}",
        session_id="ses_1",
        agent_id="lily",
    )


def test_same_message_operation_is_frontend_neutral():
    operation = {
        "kind": "message",
        "content": [{"type": "text", "text": "hello"}],
    }
    telegram = normalize_frontend_request(
        {
            "type": "hashi.frontend-request",
            "version": 1,
            "ingress": _envelope(
                source="telegram",
                transport="telegram.message",
                surface="telegram",
            ),
            "operation": operation,
        }
    )
    backend = normalize_frontend_request(
        {
            "type": "hashi.frontend-request",
            "version": 1,
            "ingress": _envelope(
                source="api",
                transport="session-api",
                surface="session-api",
            ),
            "operation": operation,
        }
    )

    assert telegram["operation"] == backend["operation"]
    assert telegram["ingress"]["connector"]["id"] == "telegram"
    assert backend["ingress"]["connector"]["id"] == "session_api"


def test_unregistered_connector_cannot_fall_through_a_generic_bypass():
    with pytest.raises(ValueError, match="unregistered frontend connector"):
        canonical_connector_id(
            "mystery-client",
            ingress_transport="mystery-wire",
            surface="mystery-ui",
        )


def test_tui_agents_override_is_registered_and_scoped_to_tui():
    customization = get_connector_customization(
        "tui",
        kind="command_override",
        key="agents",
    )
    assert customization == {
        "kind": "command_override",
        "key": "agents",
        "route": "connector_local",
        "semantic": "agent_directory",
        "reason": "local_navigation",
    }
    assert (
        get_connector_customization(
            "telegram",
            kind="command_override",
            key="agents",
        )
        is None
    )
    assert require_connector_operation("tui", "ingress", "command") == "command"


def test_telegram_local_rendering_exceptions_are_registered_in_fc():
    from orchestrator.frontend_connector_registry import (
        require_connector_presentation_override,
    )

    assert require_connector_presentation_override(
        "telegram", "voice_rendition"
    )["semantic"] == "message_voice_rendition"
    assert require_connector_presentation_override(
        "telegram", "voice_profile_preview"
    )["reason"] == "local_presentation"
    assert require_connector_presentation_override(
        "telegram", "ephemeral_progress"
    )["semantic"] == "progress_state"
    assert require_connector_presentation_override(
        "telegram", "callback_interaction_rendering"
    )["semantic"] == "command_interaction_update"
    assert require_connector_presentation_override(
        "telegram", "delivery_failover_notice"
    )["reason"] == "operational_fallback"
    with pytest.raises(ValueError, match="unregistered Connector-local"):
        require_connector_presentation_override("telegram", "random_bypass")


def test_every_declared_tui_local_command_has_an_fc_registration():
    from tui.app import TUI_COMMAND_HELP

    snapshot = connector_registry_snapshot()
    tui = next(item for item in snapshot["connectors"] if item["id"] == "tui")
    registered = {
        item["key"]
        for item in tui["customizations"]
        if item["kind"] == "command_override"
        and item["route"] == "connector_local"
    }
    assert registered == set(TUI_COMMAND_HELP)


def test_every_whatsapp_local_command_has_an_fc_registration():
    from orchestrator.frontend_compatibility import (
        require_connector_local_command,
    )
    from transports.whatsapp import WHATSAPP_LOCAL_COMMANDS

    registered = {
        require_connector_local_command("whatsapp", command)["key"]
        for command in WHATSAPP_LOCAL_COMMANDS
    }
    assert registered == set(WHATSAPP_LOCAL_COMMANDS)
    with pytest.raises(ValueError, match="unregistered Connector-local command"):
        require_connector_local_command("whatsapp", "reboot")


def test_event_exposes_standard_interface_semantics_separately_from_style_channel():
    event = normalize_frontend_event(
        {
            "type": "hashi.frontend-event",
            "version": 2,
            "event_id": "evt_1",
            "session_id": "ses_1",
            "sequence": 1,
            "durability": "durable",
            "semantic_kind": "meter",
            "presentation_channel": "meter",
            "content_blocks": [
                {"type": "text", "text": "usage", "format": "plain"},
                {
                    "type": "action",
                    "action_id": "refresh_meter",
                    "label": "Refresh",
                },
            ],
            "created_at": "2026-09-26T00:00:00Z",
        }
    )

    assert event["interface_kind"] == "display"
    assert event["presentation_channel"] == "meter"
    assert event["content_blocks"][1]["interface_kind"] == "button"


def test_dispatcher_rejects_an_unregistered_adapter_bypass(tmp_path):
    from orchestrator.frontend_connector_registry import ReferenceConnectorAdapter
    from orchestrator.frontend_dispatch import FrontendDispatcher
    from orchestrator.session_store import SessionStore

    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI1")
    with pytest.raises(ValueError, match="unknown connector"):
        FrontendDispatcher(
            store,
            adapters={
                "mystery": ReferenceConnectorAdapter(connector_id="mystery")
            },
        )


def test_standard_event_rejects_transport_specific_html():
    with pytest.raises(ValueError, match="transport-neutral"):
        normalize_frontend_event(
            {
                "type": "hashi.frontend-event",
                "version": 2,
                "event_id": "evt_html",
                "session_id": "ses_1",
                "sequence": 1,
                "durability": "durable",
                "semantic_kind": "display",
                "presentation_channel": "command",
                "content_blocks": [
                    {
                        "type": "text",
                        "text": "<b>Telegram-only</b>",
                        "format": "html",
                    }
                ],
                "created_at": "2026-09-26T00:00:00Z",
            }
        )


def test_telegram_html_is_projected_as_transport_neutral_text():
    from orchestrator.frontend_projection import (
        build_frontend_presentation_context,
        project_frontend_event,
    )

    context = build_frontend_presentation_context(
        text="<b>Status</b><br>Ready &amp; waiting",
        content_format="telegram-html",
        presentation_channel="status",
    )
    projected = project_frontend_event(
        {
            "event_id": "evt_status",
            "session_id": "ses_1",
            "sequence": 3,
            "kind": "message.created",
            "detail": {"message_id": "msg_1"},
            "created_at": "2026-09-26T00:00:00Z",
        },
        message_map={
            "msg_1": {
                "message_id": "msg_1",
                "text": "<b>Status</b><br>Ready &amp; waiting",
                "message_context": context,
            }
        },
    )

    assert projected["content_blocks"] == [
        {"type": "text", "text": "Status\nReady & waiting", "format": "plain"}
    ]


def test_public_failure_projection_carries_only_whitelisted_typed_error_code():
    from orchestrator.frontend_projection import project_frontend_event

    projected = project_frontend_event(
        {
            "event_id": "evt_auth_failed",
            "session_id": "ses_1",
            "run_id": "run_1",
            "request_id": "req_1",
            "sequence": 4,
            "kind": "run.failed",
            "summary": "Provider request failed",
            "detail": {
                "error": "raw provider text must not become structured metadata",
                "error_context": {
                    "error_code": "PROVIDER_AUTHENTICATION_FAILED",
                    "provider_request_id": "provider-private-id",
                    "diagnostic_log": "credential-shaped provider details",
                },
            },
            "created_at": "2026-10-03T03:56:08Z",
        }
    )

    assert projected["error_code"] == "PROVIDER_AUTHENTICATION_FAILED"
    assert "provider_request_id" not in projected
    assert "diagnostic_log" not in projected

    unlisted = project_frontend_event(
        {
            "event_id": "evt_private_failure",
            "session_id": "ses_1",
            "run_id": "run_2",
            "request_id": "req_2",
            "sequence": 5,
            "kind": "run.failed",
            "summary": "Provider request failed",
            "detail": {"error_context": {"error_code": "PROVIDER_RAW_SECRET_FAILURE"}},
            "created_at": "2026-10-03T03:56:09Z",
        }
    )
    assert "error_code" not in unlisted


def test_registered_compatibility_routes_are_thin_fc_adapters():
    assert get_compatibility_adapter("backend_api.agent_command") == {
        "id": "backend_api.agent_command",
        "connector_id": "backend_api",
        "direction": "ingress",
        "operation": "command",
        "route": "standard_fc",
    }
    assert get_compatibility_adapter("tui.local_command")["route"] == (
        "registered_connector_local"
    )
    assert get_compatibility_adapter("telegram.explicit_notification") == {
        "id": "telegram.explicit_notification",
        "connector_id": "telegram",
        "direction": "egress",
        "operation": "notification",
        "route": "standard_fc",
    }
    assert get_compatibility_adapter("telegram.explicit_media")["operation"] == (
        "media"
    )
    with pytest.raises(ValueError, match="unregistered compatibility adapter"):
        get_compatibility_adapter("frontend.random_bypass")


def test_explicit_telegram_tools_do_not_call_platform_http_directly():
    import inspect

    from tools.builtins import (
        execute_telegram_send,
        execute_telegram_send_file,
    )

    source = inspect.getsource(execute_telegram_send) + inspect.getsource(
        execute_telegram_send_file
    )
    assert "api.telegram.org" not in source
    assert "publish_frontend_media_notification" in source
    assert "send_long_message" in source


def test_legacy_command_routes_normalize_before_runtime_execution():
    from orchestrator.frontend_compatibility import (
        normalize_compatibility_command,
    )

    backend = normalize_compatibility_command(
        "/status",
        source_channel="workbench_api",
    )
    assert backend["adapter"]["id"] == "backend_api.agent_command"
    assert backend["operation"] == {
        "kind": "command",
        "name": "status",
        "arguments": [],
    }

    session = normalize_compatibility_command(
        "/model use gpt-5",
        source_channel="workbench_command_ui",
        session_metadata={"connector_id": "session_api"},
    )
    assert session["adapter"]["id"] == "session_api.command"
    assert session["operation"]["arguments"] == ["use", "gpt-5"]


def test_tui_local_override_cannot_leak_into_runtime_command_path():
    from orchestrator.frontend_compatibility import (
        ConnectorLocalCommand,
        normalize_compatibility_command,
    )

    with pytest.raises(ConnectorLocalCommand, match="/agents"):
        normalize_compatibility_command(
            "/agents",
            source_channel="tui",
            session_metadata={"connector_id": "tui"},
        )

    standard = normalize_compatibility_command(
        "/status",
        source_channel="tui",
        session_metadata={"connector_id": "tui"},
    )
    assert standard["adapter"]["id"] == "tui.command"


def test_whatsapp_reboot_uses_standard_command_adapter():
    from orchestrator.frontend_compatibility import (
        ConnectorLocalCommand,
        normalize_compatibility_command,
    )

    standard = normalize_compatibility_command(
        "/reboot same",
        source_channel="whatsapp_forwarded",
        session_metadata={"connector_id": "whatsapp"},
    )
    assert standard["adapter"]["id"] == "whatsapp.native_command"
    assert standard["operation"] == {
        "kind": "command",
        "name": "reboot",
        "arguments": ["same"],
    }
    with pytest.raises(ConnectorLocalCommand, match="/agent"):
        normalize_compatibility_command(
            "/agent lily",
            source_channel="whatsapp_forwarded",
            session_metadata={"connector_id": "whatsapp"},
        )


def test_control_and_button_compatibility_routes_share_standard_validation():
    from orchestrator.frontend_compatibility import (
        normalize_compatibility_operation,
    )

    control = normalize_compatibility_operation(
        "session_api.control",
        {"kind": "control", "action": "cancel", "target_run_id": "run_1"},
    )
    assert control["operation"] == {
        "kind": "control",
        "action": "cancel",
        "target_run_id": "run_1",
        "text": None,
    }
    action = normalize_compatibility_operation(
        "session_api.action",
        {"kind": "action", "action_id": "approve_1", "revision": 1},
    )
    assert action["operation"]["kind"] == "action"
    with pytest.raises(ValueError, match="operation mismatch"):
        normalize_compatibility_operation(
            "session_api.command",
            {"kind": "control", "action": "cancel"},
        )


def test_connector_event_validation_covers_buttons_and_media():
    event = normalize_frontend_event(
        {
            "type": "hashi.frontend-event",
            "version": 2,
            "event_id": "evt_components",
            "session_id": "ses_1",
            "sequence": 4,
            "durability": "durable",
            "semantic_kind": "display",
            "presentation_channel": "command",
            "content_blocks": [
                {"type": "text", "text": "Choose", "format": "plain"},
                {
                    "type": "action",
                    "action_id": "continue_action",
                    "label": "Continue",
                },
                {
                    "type": "media_ref",
                    "group_id": "group_1",
                    "attachment_id": "attachment_1",
                    "role": "document",
                },
            ],
            "created_at": "2026-09-26T00:00:00Z",
        }
    )
    assert require_connector_event("reference", event) == {
        "display",
        "button",
        "media",
    }


@pytest.mark.asyncio
async def test_transport_adapter_receives_only_standard_event_and_returns_receipt():
    from orchestrator.frontend_dispatch import OutcomeConnectorAdapter

    observed = []

    async def send(event, *, endpoint_id):
        observed.append((event, endpoint_id))
        return {
            "attempted": True,
            "state": "queued",
            "delivered": False,
            "message_id": "reply_123",
        }

    adapter = OutcomeConnectorAdapter("exchange", send)
    event = normalize_frontend_event(
        {
            "type": "hashi.frontend-event",
            "version": 2,
            "event_id": "evt_exchange",
            "session_id": "ses_1",
            "sequence": 8,
            "durability": "durable",
            "semantic_kind": "message",
            "presentation_channel": "final",
            "content_blocks": [
                {"type": "text", "text": "Reply", "format": "plain"}
            ],
            "created_at": "2026-09-26T00:00:00Z",
        }
    )
    receipt = await adapter.dispatch(event, endpoint_id="ep_exchange")

    assert observed == [(event, "ep_exchange")]
    assert receipt == {
        "type": "hashi.delivery-receipt",
        "version": 1,
        "event_id": "evt_exchange",
        "endpoint_id": "ep_exchange",
        "status": "accepted",
        "proof": None,
    }


@pytest.mark.parametrize("surface", ["remote", "exchange"])
def test_authenticated_relays_keep_their_connector_identity(surface):
    from orchestrator.frontend_delivery import freeze_run_delivery_route

    route = freeze_run_delivery_route(
        message_source_id="hchat",
        session_surface=surface,
        session_channel_key=f"peer:{surface}",
        chat_id=7,
        telegram_requested=False,
    )
    assert route["primary"] == {
        "surface": surface,
        "channel_key": f"peer:{surface}",
    }
