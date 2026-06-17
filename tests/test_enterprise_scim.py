from __future__ import annotations

from orchestrator.enterprise import EnterpriseRole, IdentityService, ScimProvisioningService


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
