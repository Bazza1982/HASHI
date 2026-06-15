from __future__ import annotations

import json
from types import SimpleNamespace

from orchestrator.enterprise import AuditEventWriter, ChannelRegistry, EnterpriseChannelGate, IdentityService, PolicyEvaluator


def _global_config(tmp_path, *, profile: str = "enterprise", org_id: str | None = "ORG-001"):
    return SimpleNamespace(
        deployment_profile=profile,
        organization_id=org_id,
        bridge_home=tmp_path,
    )


def _audit_events(tmp_path) -> list[dict]:
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_personal_profile_channel_gate_allows_without_registry(tmp_path):
    gate = EnterpriseChannelGate.from_global_config(_global_config(tmp_path, profile="personal", org_id=None))

    result = gate.check_ingress("telegram", actor_id="owner")

    assert result.allowed is True
    assert result.reason == "personal_profile"


def test_governed_channel_gate_denies_disabled_default_and_audits(tmp_path):
    identity = IdentityService.from_path(tmp_path / "state" / "enterprise.sqlite")
    identity.create_organization(org_id="ORG-001", name="Acme")
    audit_writer = AuditEventWriter(enabled=True, jsonl_path=tmp_path / "audit.jsonl")
    gate = EnterpriseChannelGate.from_global_config(_global_config(tmp_path), audit_writer=audit_writer)

    result = gate.check_ingress(
        "telegram",
        actor_id="usr-1",
        project_id="prj-research",
        audit_context={"source": "telegram"},
    )

    assert result.allowed is False
    assert result.reason == "channel_disabled"
    event = _audit_events(tmp_path)[-1]
    assert event["event_type"] == "channel"
    assert event["action"] == "channel_access"
    assert event["status"] == "denied"
    assert event["context"]["channel_type"] == "telegram"
    assert event["context"]["source"] == "telegram"


def test_governed_channel_gate_allows_enabled_bound_project(tmp_path):
    identity = IdentityService.from_path(tmp_path / "state" / "enterprise.sqlite")
    identity.create_organization(org_id="ORG-001", name="Acme")
    registry = ChannelRegistry.from_path(tmp_path / "state" / "enterprise.sqlite")
    registry.ensure_default_channels(org_id="ORG-001")
    registry.register_channel(org_id="ORG-001", channel_type="hchat", enabled=True)
    registry.bind_channel(org_id="ORG-001", channel_type="hchat", scope_type="project", scope_id="prj-research")
    audit_writer = AuditEventWriter(enabled=True, jsonl_path=tmp_path / "audit.jsonl")
    gate = EnterpriseChannelGate.from_global_config(_global_config(tmp_path), audit_writer=audit_writer)

    result = gate.check_egress("hchat", actor_id="usr-1", project_id="prj-research")

    assert result.allowed is True
    assert result.reason == "allowed"
    assert _audit_events(tmp_path) == []


def test_governed_channel_gate_applies_policy_deny_after_registry_allow(tmp_path):
    identity = IdentityService.from_path(tmp_path / "state" / "enterprise.sqlite")
    identity.create_organization(org_id="ORG-001", name="Acme")
    registry = ChannelRegistry.from_path(tmp_path / "state" / "enterprise.sqlite")
    registry.ensure_default_channels(org_id="ORG-001")
    registry.register_channel(org_id="ORG-001", channel_type="hchat", enabled=True)
    registry.bind_channel(org_id="ORG-001", channel_type="hchat", scope_type="project", scope_id="prj-research")
    policy = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    policy.add_rule(
        action="channel.access",
        resource="channel:hchat",
        effect="deny",
        conditions={"direction": "egress"},
    )
    audit_writer = AuditEventWriter(enabled=True, jsonl_path=tmp_path / "audit.jsonl")
    gate = EnterpriseChannelGate.from_global_config(_global_config(tmp_path), audit_writer=audit_writer)

    result = gate.check_egress("hchat", actor_id="usr-1", project_id="prj-research")

    assert result.allowed is False
    assert result.reason == "policy_denied"
    event = _audit_events(tmp_path)[-1]
    assert event["context"]["reason"] == "policy_denied"
    assert event["context"]["policy_reason"] == "policy_denied"
    assert event["context"]["channel_type"] == "hchat"


def test_governed_channel_gate_blocks_approval_required_policy(tmp_path):
    identity = IdentityService.from_path(tmp_path / "state" / "enterprise.sqlite")
    identity.create_organization(org_id="ORG-001", name="Acme")
    registry = ChannelRegistry.from_path(tmp_path / "state" / "enterprise.sqlite")
    registry.ensure_default_channels(org_id="ORG-001")
    registry.register_channel(org_id="ORG-001", channel_type="telegram", enabled=True)
    registry.bind_channel(org_id="ORG-001", channel_type="telegram", scope_type="user", scope_id="usr-1")
    policy = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    policy.add_rule(
        action="channel.access",
        resource="channel:telegram",
        effect="approval_required",
        conditions={"direction": "ingress"},
    )
    audit_writer = AuditEventWriter(enabled=True, jsonl_path=tmp_path / "audit.jsonl")
    gate = EnterpriseChannelGate.from_global_config(_global_config(tmp_path), audit_writer=audit_writer)

    result = gate.check_ingress("telegram", actor_id="usr-1", user_id="usr-1")

    assert result.allowed is False
    assert result.reason == "approval_required"
    assert _audit_events(tmp_path)[-1]["context"]["reason"] == "approval_required"


def test_governed_channel_gate_missing_org_id_fails_closed(tmp_path):
    audit_writer = AuditEventWriter(enabled=True, jsonl_path=tmp_path / "audit.jsonl")
    gate = EnterpriseChannelGate.from_global_config(
        _global_config(tmp_path, profile="enterprise", org_id=None),
        audit_writer=audit_writer,
    )

    result = gate.check_ingress("workbench", actor_id="usr-1")

    assert result.allowed is False
    assert result.reason == "missing_organization_id"
    assert _audit_events(tmp_path)[-1]["context"]["reason"] == "missing_organization_id"
