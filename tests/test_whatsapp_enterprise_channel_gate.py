from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator.enterprise import (
    AuditEventWriter,
    ChannelRegistry,
    EnterpriseChannelGate,
    IdentityService,
)
from transports.whatsapp import WhatsAppTransport


def _transport(tmp_path, *, profile: str = "enterprise", org_id: str | None = "ORG-001"):
    transport = WhatsAppTransport.__new__(WhatsAppTransport)
    transport.global_cfg = SimpleNamespace(
        deployment_profile=profile,
        organization_id=org_id,
        bridge_home=tmp_path,
    )
    transport.sent = []

    async def _send_text(chat_key: str, text: str):
        transport.sent.append((chat_key, text))

    transport._send_text = _send_text
    transport._get_runtime = lambda _agent_name: None
    audit_writer = AuditEventWriter(enabled=True, jsonl_path=tmp_path / "state" / "enterprise_audit.jsonl")
    transport._channel_gate = EnterpriseChannelGate.from_global_config(
        transport.global_cfg,
        audit_writer=audit_writer,
    )
    return transport


def _audit_events(tmp_path) -> list[dict]:
    path = tmp_path / "state" / "enterprise_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.parametrize(
    ("socket_connected", "logged_in", "expected"),
    [
        (True, True, True),
        (True, False, False),
        (False, True, False),
    ],
)
def test_whatsapp_connection_state_requires_socket_and_login(
    socket_connected, logged_in, expected
):
    transport = WhatsAppTransport.__new__(WhatsAppTransport)
    transport._client = SimpleNamespace(
        is_connected=lambda: socket_connected,
        is_logged_in=lambda: logged_in,
    )

    assert transport.is_connected() is expected


@pytest.mark.asyncio
async def test_personal_whatsapp_ingress_gate_allows(tmp_path):
    transport = _transport(tmp_path, profile="personal", org_id=None)

    allowed = await transport._check_whatsapp_ingress_allowed(
        chat_key="61400000000@s.whatsapp.net",
        phone="+61400000000",
    )

    assert allowed is True
    assert transport.sent == []


@pytest.mark.asyncio
async def test_enterprise_whatsapp_ingress_denies_disabled_default_and_audits(tmp_path):
    identity = IdentityService.from_path(tmp_path / "state" / "enterprise.sqlite")
    identity.create_organization(org_id="ORG-001", name="Acme")
    transport = _transport(tmp_path)

    allowed = await transport._check_whatsapp_ingress_allowed(
        chat_key="61400000000@s.whatsapp.net",
        phone="+61400000000",
    )

    assert allowed is False
    assert transport.sent == [
        (
            "61400000000@s.whatsapp.net",
            "WhatsApp access is not enabled for this enterprise HASHI workspace.",
        )
    ]
    event = _audit_events(tmp_path)[-1]
    assert event["event_type"] == "channel"
    assert event["status"] == "denied"
    assert event["context"]["channel_type"] == "whatsapp"
    assert event["context"]["reason"] == "channel_disabled"
    assert event["context"]["chat_id"] == "61400000000@s.whatsapp.net"


@pytest.mark.asyncio
async def test_enterprise_whatsapp_ingress_allows_bound_phone(tmp_path):
    identity = IdentityService.from_path(tmp_path / "state" / "enterprise.sqlite")
    identity.create_organization(org_id="ORG-001", name="Acme")
    registry = ChannelRegistry.from_path(tmp_path / "state" / "enterprise.sqlite")
    registry.ensure_default_channels(org_id="ORG-001")
    registry.register_channel(org_id="ORG-001", channel_type="whatsapp", enabled=True)
    registry.bind_channel(
        org_id="ORG-001",
        channel_type="whatsapp",
        scope_type="user",
        scope_id="+61400000000",
        permission="ingress",
    )
    transport = _transport(tmp_path)

    allowed = await transport._check_whatsapp_ingress_allowed(
        chat_key="61400000000@s.whatsapp.net",
        phone="+61400000000",
    )

    assert allowed is True
    assert transport.sent == []
    assert _audit_events(tmp_path) == []


@pytest.mark.asyncio
async def test_enterprise_whatsapp_egress_denies_disabled_default_and_audits(tmp_path):
    identity = IdentityService.from_path(tmp_path / "state" / "enterprise.sqlite")
    identity.create_organization(org_id="ORG-001", name="Acme")
    transport = _transport(tmp_path)

    await transport._on_agent_response(
        "61400000000@s.whatsapp.net",
        "nana",
        {"success": True, "text": "hello"},
        "single",
        ["nana"],
    )

    assert transport.sent == []
    event = _audit_events(tmp_path)[-1]
    assert event["event_type"] == "channel"
    assert event["status"] == "denied"
    assert event["context"]["channel_type"] == "whatsapp"
    assert event["context"]["direction"] == "egress"
    assert event["context"]["reason"] == "channel_disabled"


@pytest.mark.asyncio
async def test_enterprise_whatsapp_egress_allows_bound_agent(tmp_path):
    identity = IdentityService.from_path(tmp_path / "state" / "enterprise.sqlite")
    identity.create_organization(org_id="ORG-001", name="Acme")
    registry = ChannelRegistry.from_path(tmp_path / "state" / "enterprise.sqlite")
    registry.ensure_default_channels(org_id="ORG-001")
    registry.register_channel(org_id="ORG-001", channel_type="whatsapp", enabled=True)
    registry.bind_channel(
        org_id="ORG-001",
        channel_type="whatsapp",
        scope_type="agent",
        scope_id="nana",
        permission="egress",
    )
    transport = _transport(tmp_path)

    await transport._on_agent_response(
        "61400000000@s.whatsapp.net",
        "nana",
        {"success": True, "text": "hello"},
        "single",
        ["nana"],
    )

    assert transport.sent == [("61400000000@s.whatsapp.net", "[nana]: hello")]
    assert _audit_events(tmp_path) == []


@pytest.mark.asyncio
async def test_whatsapp_ingress_passes_chat_key_to_session_routing(tmp_path):
    captured = []

    class Runtime:
        name = "nana"

        async def enqueue_request(self, **kwargs):
            captured.append(kwargs)
            return "req-whatsapp"

        def register_request_listener(self, request_id, callback):
            self.listener = (request_id, callback)

    runtime = Runtime()
    transport = WhatsAppTransport.__new__(WhatsAppTransport)
    transport._refresh_runtime_config = lambda: None
    transport._allowed_numbers = set()
    transport._allowed_chat_ids = set()
    transport._jid_cache = {}
    transport._jid_str = lambda jid: str(jid)
    transport._phone_candidates = lambda _jid: {"+61400000000"}

    async def allow_ingress(**_kwargs):
        return True

    transport._check_whatsapp_ingress_allowed = allow_ingress
    transport._extract_text = lambda _msg: "hello"
    transport._is_voice = lambda _msg: False
    transport._detect_media_kind = lambda _msg: None
    transport._router = SimpleNamespace(
        get_targets=lambda _chat: ["nana"],
        get_mode=lambda _chat: "single",
    )
    transport.orchestrator = SimpleNamespace(runtimes=[runtime])
    transport._get_runtime = lambda name: runtime if name == "nana" else None

    async def send_text(_chat_key, _text):
        raise AssertionError("no reply should be sent before the Agent completes")

    transport._send_text = send_text
    chat_key = "61400000000@s.whatsapp.net"
    message = SimpleNamespace(
        Info=SimpleNamespace(
            MessageSource=SimpleNamespace(
                IsFromMe=False,
                Chat=chat_key,
                Sender=chat_key,
            )
        )
    )

    await transport._on_message(message)

    assert captured[0]["request_metadata"] == {
        "session_surface": "whatsapp",
        "session_channel_key": chat_key,
    }
