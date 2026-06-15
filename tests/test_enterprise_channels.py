from __future__ import annotations

from orchestrator.enterprise import (
    ChannelPermission,
    ChannelRegistry,
    ChannelScopeType,
    ChannelType,
    IdentityService,
)


def _registry(tmp_path) -> ChannelRegistry:
    return ChannelRegistry.from_path(tmp_path / "enterprise.sqlite")


def test_channels_are_registered_disabled_by_default(tmp_path):
    identity = IdentityService.from_path(tmp_path / "enterprise.sqlite")
    identity.create_organization(org_id="ORG-001", name="Acme")
    registry = _registry(tmp_path)

    channel = registry.register_channel(
        org_id="ORG-001",
        channel_type=ChannelType.TEAMS,
        display_name="Microsoft Teams",
        config={"tenant": "acme"},
        risk_tier="high",
    )

    assert channel.type == "teams"
    assert channel.enabled is False
    assert channel.config == {"tenant": "acme"}
    assert registry.check_access(
        org_id="ORG-001",
        channel_type=ChannelType.TEAMS,
        direction=ChannelPermission.INGRESS,
        project_id="prj-research",
    ).reason == "channel_disabled"


def test_enabled_channel_requires_explicit_binding(tmp_path):
    identity = IdentityService.from_path(tmp_path / "enterprise.sqlite")
    identity.create_organization(org_id="ORG-001", name="Acme")
    registry = _registry(tmp_path)
    registry.register_channel(org_id="ORG-001", channel_type=ChannelType.SLACK, enabled=True)

    access = registry.check_access(
        org_id="ORG-001",
        channel_type=ChannelType.SLACK,
        direction=ChannelPermission.INGRESS,
        project_id="prj-research",
    )

    assert access.allowed is False
    assert access.reason == "channel_not_bound"


def test_channel_project_binding_allows_matching_direction(tmp_path):
    identity = IdentityService.from_path(tmp_path / "enterprise.sqlite")
    identity.create_organization(org_id="ORG-001", name="Acme")
    registry = _registry(tmp_path)
    channel = registry.register_channel(org_id="ORG-001", channel_type=ChannelType.HCHAT, enabled=True)
    registry.bind_channel(
        org_id="ORG-001",
        channel_type=ChannelType.HCHAT,
        scope_type=ChannelScopeType.PROJECT,
        scope_id="prj-research",
        permission=ChannelPermission.BOTH,
    )

    access = registry.check_access(
        org_id="ORG-001",
        channel_type=ChannelType.HCHAT,
        direction=ChannelPermission.EGRESS,
        project_id="prj-research",
    )
    denied = registry.check_access(
        org_id="ORG-001",
        channel_type=ChannelType.HCHAT,
        direction=ChannelPermission.EGRESS,
        project_id="prj-finance",
    )

    assert access.allowed is True
    assert access.channel_id == channel.id
    assert denied.allowed is False
    assert denied.reason == "channel_not_bound"


def test_channel_binding_can_scope_to_agent_or_user(tmp_path):
    identity = IdentityService.from_path(tmp_path / "enterprise.sqlite")
    identity.create_organization(org_id="ORG-001", name="Acme")
    registry = _registry(tmp_path)
    registry.register_channel(org_id="ORG-001", channel_type=ChannelType.WORKBENCH, enabled=True)
    registry.bind_channel(
        org_id="ORG-001",
        channel_type=ChannelType.WORKBENCH,
        scope_type=ChannelScopeType.USER,
        scope_id="usr-1",
        permission=ChannelPermission.INGRESS,
    )
    registry.bind_channel(
        org_id="ORG-001",
        channel_type=ChannelType.WORKBENCH,
        scope_type=ChannelScopeType.AGENT,
        scope_id="nana",
        permission=ChannelPermission.EGRESS,
    )

    assert registry.check_access(
        org_id="ORG-001",
        channel_type=ChannelType.WORKBENCH,
        direction=ChannelPermission.INGRESS,
        user_id="usr-1",
    ).allowed
    assert registry.check_access(
        org_id="ORG-001",
        channel_type=ChannelType.WORKBENCH,
        direction=ChannelPermission.EGRESS,
        agent_id="nana",
    ).allowed
    assert registry.check_access(
        org_id="ORG-001",
        channel_type=ChannelType.WORKBENCH,
        direction=ChannelPermission.EGRESS,
        user_id="usr-1",
    ).reason == "channel_not_bound"


def test_unregistered_channel_fails_closed(tmp_path):
    identity = IdentityService.from_path(tmp_path / "enterprise.sqlite")
    identity.create_organization(org_id="ORG-001", name="Acme")
    registry = _registry(tmp_path)

    access = registry.check_access(
        org_id="ORG-001",
        channel_type=ChannelType.FEISHU,
        direction=ChannelPermission.INGRESS,
        project_id="prj-research",
    )

    assert access.allowed is False
    assert access.reason == "channel_not_registered"


def test_default_channels_seed_control_plane_without_opening_external_channels(tmp_path):
    identity = IdentityService.from_path(tmp_path / "enterprise.sqlite")
    identity.create_organization(org_id="ORG-001", name="Acme")
    registry = _registry(tmp_path)

    channels = registry.ensure_default_channels(org_id="ORG-001")

    by_type = {channel.type: channel for channel in channels}
    assert by_type["workbench"].enabled is True
    assert by_type["workbench"].risk_tier == "low"
    for channel_type in ["hchat", "telegram", "whatsapp", "email", "slack", "teams", "google_chat", "feishu"]:
        assert by_type[channel_type].enabled is False
    assert registry.check_access(
        org_id="ORG-001",
        channel_type=ChannelType.TELEGRAM,
        direction=ChannelPermission.INGRESS,
        project_id="prj-research",
    ).reason == "channel_disabled"
