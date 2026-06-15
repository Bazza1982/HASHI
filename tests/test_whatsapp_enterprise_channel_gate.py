from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator.enterprise import AuditEventWriter, ChannelRegistry, EnterpriseChannelGate, IdentityService
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
