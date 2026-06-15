from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from orchestrator.enterprise import ChannelRegistry, IdentityService
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime


def _runtime(tmp_path):
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.name = "nana"
    runtime.global_config = SimpleNamespace(
        deployment_profile="enterprise",
        organization_id="ORG-001",
        bridge_home=tmp_path,
    )
    runtime.logger = Mock()
    runtime._reply_text = AsyncMock()
    return runtime


def _update(user_id: int = 123, chat_id: int = 456):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        message=SimpleNamespace(text="hello"),
    )


def _audit_events(tmp_path) -> list[dict]:
    path = tmp_path / "state" / "enterprise_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_enterprise_telegram_gate_denies_disabled_default_and_audits(tmp_path):
    IdentityService.from_path(tmp_path / "state" / "enterprise.sqlite").create_organization(
        org_id="ORG-001",
        name="Acme",
    )
    runtime = _runtime(tmp_path)

    allowed = await FlexibleAgentRuntime._telegram_channel_allowed(runtime, _update(), source_channel="telegram")

    assert allowed is False
    runtime._reply_text.assert_awaited_once()
    event = _audit_events(tmp_path)[-1]
    assert event["event_type"] == "channel"
    assert event["status"] == "denied"
    assert event["context"]["channel_type"] == "telegram"
    assert event["context"]["reason"] == "channel_disabled"
    assert event["context"]["chat_id"] == 456


@pytest.mark.asyncio
async def test_enterprise_telegram_gate_allows_bound_agent(tmp_path):
    IdentityService.from_path(tmp_path / "state" / "enterprise.sqlite").create_organization(
        org_id="ORG-001",
        name="Acme",
    )
    registry = ChannelRegistry.from_path(tmp_path / "state" / "enterprise.sqlite")
    registry.ensure_default_channels(org_id="ORG-001")
    registry.register_channel(org_id="ORG-001", channel_type="telegram", enabled=True)
    registry.bind_channel(
        org_id="ORG-001",
        channel_type="telegram",
        scope_type="agent",
        scope_id="nana",
        permission="ingress",
    )
    runtime = _runtime(tmp_path)

    allowed = await FlexibleAgentRuntime._telegram_channel_allowed(runtime, _update(), source_channel="telegram")

    assert allowed is True
    runtime._reply_text.assert_not_awaited()
    assert _audit_events(tmp_path) == []


@pytest.mark.asyncio
async def test_personal_telegram_gate_allows_without_registry(tmp_path):
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.name = "nana"
    runtime.global_config = SimpleNamespace(deployment_profile="personal", bridge_home=tmp_path)
    runtime.logger = Mock()
    runtime._reply_text = AsyncMock()

    allowed = await FlexibleAgentRuntime._telegram_channel_allowed(runtime, _update(), source_channel="telegram")

    assert allowed is True
    runtime._reply_text.assert_not_awaited()
