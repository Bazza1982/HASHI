from __future__ import annotations

from orchestrator.enterprise import EnterpriseRole, IdentityService, ScimProvisioningService
from orchestrator.enterprise.scim import (
    SCIM_GROUP_SCHEMA,
    SCIM_LIST_RESPONSE_SCHEMA,
    SCIM_USER_SCHEMA,
    filter_scim_groups,
    filter_scim_users,
)


def _services(tmp_path):
    identity = IdentityService.from_path(tmp_path / "enterprise.sqlite")
    identity.create_organization(org_id="ORG-001", name="Acme")
    project = identity.create_project(org_id="ORG-001", name="Default", project_id="prj-default")
    return identity, ScimProvisioningService(identity), project


def test_scim_upsert_creates_active_individual_user_with_membership(tmp_path):
    identity, scim, project = _services(tmp_path)

    result = scim.upsert_user(
        org_id="ORG-001",
        payload={
            "userName": "User@Example.com",
            "displayName": "SCIM User",
            "externalId": "idp-123",
            "active": True,
        },
        default_project_id=project.id,
    )

    assert result.created is True
    assert result.action == "created"
    assert result.external_id == "idp-123"
    assert result.user.email == "user@example.com"
    assert result.user.status == "active"
    assert identity.list_project_memberships(user_id=result.user.id)[0]["role"] == EnterpriseRole.INDIVIDUAL_USER.value


def test_scim_upsert_updates_existing_user_without_creating_duplicate(tmp_path):
    identity, scim, _ = _services(tmp_path)
    first = scim.upsert_user(
        org_id="ORG-001",
        payload={"userName": "user@example.com", "displayName": "Old Name"},
    )

    second = scim.upsert_user(
        org_id="ORG-001",
        payload={"userName": "USER@example.com", "displayName": "New Name"},
    )

    assert second.created is False
    assert second.action == "updated"
    assert second.user.id == first.user.id
    assert second.user.display_name == "New Name"
    assert len(identity.list_users(org_id="ORG-001")) == 1


def test_scim_deactivate_disables_user_and_revokes_sessions_and_tokens(tmp_path):
    identity, scim, _ = _services(tmp_path)
    result = scim.upsert_user(
        org_id="ORG-001",
        payload={"userName": "user@example.com", "displayName": "SCIM User"},
    )
    session = identity.create_session(user_id=result.user.id)
    token = identity.create_api_token(user_id=result.user.id, scopes=["audit:read"])

    deactivated = scim.upsert_user(
        org_id="ORG-001",
        payload={"userName": "user@example.com", "displayName": "SCIM User", "active": False},
    )

    assert deactivated.action == "deactivated"
    assert deactivated.user.status == "disabled"
    assert identity.get_session_user(session.token) is None
    assert identity.validate_api_token(token.token) is None


def test_scim_can_reactivate_disabled_user_without_restoring_old_tokens(tmp_path):
    identity, scim, _ = _services(tmp_path)
    result = scim.upsert_user(org_id="ORG-001", payload={"userName": "user@example.com", "active": True})
    token = identity.create_api_token(user_id=result.user.id, scopes=["audit:read"])
    scim.deactivate_user(org_id="ORG-001", user_name="user@example.com")

    reactivated = scim.upsert_user(org_id="ORG-001", payload={"userName": "user@example.com", "active": True})

    assert reactivated.user.status == "active"
    assert identity.validate_api_token(token.token) is None


def test_scim_extracts_email_and_name_from_standard_fields(tmp_path):
    _, scim, _ = _services(tmp_path)

    result = scim.upsert_user(
        org_id="ORG-001",
        payload={
            "emails": [{"value": "fallback@example.com"}],
            "name": {"givenName": "Fallback", "familyName": "User"},
        },
    )

    assert result.user.email == "fallback@example.com"
    assert result.user.display_name == "Fallback User"


def test_scim_requires_identity_email():
    identity = IdentityService.from_path(":memory:")
    scim = ScimProvisioningService(identity)

    try:
        scim.upsert_user(org_id="ORG-001", payload={})
    except ValueError as exc:
        assert "SCIM userName" in str(exc)
    else:
        raise AssertionError("expected missing SCIM identity to fail")


def test_scim_v2_list_response_supports_filter_and_pagination(tmp_path):
    identity, scim, _ = _services(tmp_path)
    scim.upsert_user(org_id="ORG-001", payload={"userName": "alpha@example.com", "displayName": "Alpha"})
    scim.upsert_user(org_id="ORG-001", payload={"userName": "beta@example.com", "displayName": "Beta"})

    page = scim.list_user_resources(org_id="ORG-001", start_index=2, count=1)
    filtered = scim.list_user_resources(
        org_id="ORG-001",
        filter_expression='userName eq "beta@example.com"',
    )

    assert page["schemas"] == [SCIM_LIST_RESPONSE_SCHEMA]
    assert page["totalResults"] == 2
    assert page["itemsPerPage"] == 1
    assert page["Resources"][0]["schemas"] == [SCIM_USER_SCHEMA]
    assert page["Resources"][0]["userName"] == "beta@example.com"
    assert filtered["totalResults"] == 1
    assert filtered["Resources"][0]["displayName"] == "Beta"


def test_scim_v2_filter_supports_active_and_rejects_unknown_filter(tmp_path):
    identity, scim, _ = _services(tmp_path)
    active = scim.upsert_user(org_id="ORG-001", payload={"userName": "active@example.com"})
    disabled = scim.upsert_user(org_id="ORG-001", payload={"userName": "disabled@example.com", "active": False})

    active_only = filter_scim_users([active.user, disabled.user], "active eq true")

    assert [user.email for user in active_only] == ["active@example.com"]
    try:
        filter_scim_users([active.user], 'displayName co "Active"')
    except ValueError as exc:
        assert "unsupported SCIM filter" in str(exc)
    else:
        raise AssertionError("expected unsupported filter to fail")


def test_scim_v2_patch_updates_display_name_and_deactivates(tmp_path):
    identity, scim, _ = _services(tmp_path)
    result = scim.upsert_user(org_id="ORG-001", payload={"userName": "user@example.com", "displayName": "Old"})
    token = identity.create_api_token(user_id=result.user.id, scopes=["audit:read"])

    updated = scim.patch_user(
        user_id=result.user.id,
        payload={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "displayName", "value": "New"}],
        },
    )
    deactivated = scim.patch_user(
        user_id=result.user.id,
        payload={"Operations": [{"op": "replace", "path": "active", "value": "false"}]},
    )

    assert updated.user.display_name == "New"
    assert deactivated.action == "deactivated"
    assert deactivated.user.status == "disabled"
    assert identity.validate_api_token(token.token) is None


def test_scim_v2_groups_map_projects_to_groups_with_active_members(tmp_path):
    identity, scim, project = _services(tmp_path)
    active = scim.upsert_user(
        org_id="ORG-001",
        payload={"userName": "active@example.com", "displayName": "Active User"},
        default_project_id=project.id,
    )
    disabled = scim.upsert_user(
        org_id="ORG-001",
        payload={"userName": "disabled@example.com", "displayName": "Disabled User"},
        default_project_id=project.id,
    )
    identity.deactivate_user(user_id=disabled.user.id)

    groups = scim.list_group_resources(org_id="ORG-001")
    group = scim.get_group_resource(org_id="ORG-001", group_id=project.id)

    assert groups["schemas"] == [SCIM_LIST_RESPONSE_SCHEMA]
    assert groups["totalResults"] == 1
    assert group["schemas"] == [SCIM_GROUP_SCHEMA]
    assert group["id"] == "prj-default"
    assert group["displayName"] == "Default"
    assert group["members"] == [
        {"value": active.user.id, "display": "Active User", "$ref": f"/scim/v2/Users/{active.user.id}"}
    ]


def test_scim_v2_groups_support_filter_and_reject_unknown_filter(tmp_path):
    identity, scim, project = _services(tmp_path)
    identity.create_project(org_id="ORG-001", name="Research", project_id="prj-research")

    by_name = scim.list_group_resources(org_id="ORG-001", filter_expression='displayName eq "Research"')
    by_id = filter_scim_groups(identity.list_projects(org_id="ORG-001"), 'id eq "prj-default"')

    assert by_name["totalResults"] == 1
    assert by_name["Resources"][0]["id"] == "prj-research"
    assert [group.id for group in by_id] == [project.id]
    try:
        filter_scim_groups(identity.list_projects(org_id="ORG-001"), 'displayName co "Def"')
    except ValueError as exc:
        assert "unsupported SCIM group filter" in str(exc)
    else:
        raise AssertionError("expected unsupported group filter to fail")
