from __future__ import annotations

import sqlite3

import pytest

from orchestrator.enterprise import EnterpriseRole, IdentityService


def _service(tmp_path) -> IdentityService:
    return IdentityService.from_path(tmp_path / "enterprise.sqlite")


def test_bootstrap_org_admin_creates_org_default_project_and_membership(tmp_path):
    service = _service(tmp_path)

    user = service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="Admin@Example.COM",
        display_name="Admin User",
        password="correct horse battery staple",
        user_id="usr-admin",
    )

    assert user.email == "admin@example.com"
    assert service.get_organization("ORG-001").name == "Acme"
    assert service.authenticate_user(org_id="ORG-001", email="admin@example.com", password="wrong") is None
    assert service.authenticate_user(org_id="ORG-001", email="admin@example.com", password="correct horse battery staple") == user
    assert service.list_project_memberships(user_id="usr-admin") == [
        {
            "project_id": "ORG-001-default",
            "role": EnterpriseRole.ORG_ADMIN.value,
            "created_at": service.list_project_memberships(user_id="usr-admin")[0]["created_at"],
        }
    ]


def test_bootstrap_rejects_existing_bootstrapped_org(tmp_path):
    service = _service(tmp_path)
    kwargs = {
        "org_id": "ORG-001",
        "org_name": "Acme",
        "email": "admin@example.com",
        "display_name": "Admin",
        "password": "secret-password",
    }

    service.bootstrap_org_admin(**kwargs)

    with pytest.raises(ValueError, match="already bootstrapped"):
        service.bootstrap_org_admin(**kwargs)


def test_password_hash_is_not_plaintext(tmp_path):
    service = _service(tmp_path)
    service.create_organization(org_id="ORG-001", name="Acme")
    service.create_user(
        org_id="ORG-001",
        email="user@example.com",
        display_name="User",
        password="secret-password",
        user_id="usr-1",
    )

    with sqlite3.connect(tmp_path / "enterprise.sqlite") as con:
        stored = con.execute("SELECT password_hash FROM users WHERE id = 'usr-1'").fetchone()[0]

    assert "secret-password" not in stored
    assert stored.startswith("pbkdf2_sha256$")


def test_sessions_can_be_created_validated_and_revoked(tmp_path):
    service = _service(tmp_path)
    service.create_organization(org_id="ORG-001", name="Acme")
    user = service.create_user(
        org_id="ORG-001",
        email="user@example.com",
        display_name="User",
        password="secret-password",
        user_id="usr-1",
    )

    session = service.create_session(user_id=user.id, ttl_seconds=60)

    assert session.token.startswith("hs_sess_")
    assert service.get_session_user(session.token) == user
    assert service.revoke_session(session.token) is True
    assert service.get_session_user(session.token) is None
    assert service.revoke_session(session.token) is False


def test_project_membership_role_assignment_is_upserted(tmp_path):
    service = _service(tmp_path)
    service.create_organization(org_id="ORG-001", name="Acme")
    user = service.create_user(
        org_id="ORG-001",
        email="user@example.com",
        display_name="User",
        password="secret-password",
        user_id="usr-1",
    )
    project = service.create_project(
        org_id="ORG-001",
        name="Research",
        workspace_root="/srv/hashi/workspaces/research",
        project_id="prj-research",
    )

    service.assign_project_role(user_id=user.id, project_id=project.id, role=EnterpriseRole.INDIVIDUAL_USER)
    service.assign_project_role(user_id=user.id, project_id=project.id, role=EnterpriseRole.TEAM_ADMIN)

    memberships = service.list_project_memberships(user_id=user.id)
    assert len(memberships) == 1
    assert memberships[0]["project_id"] == "prj-research"
    assert memberships[0]["role"] == "team_admin"


def test_api_tokens_are_hashed_validated_and_revoked(tmp_path):
    service = _service(tmp_path)
    service.create_organization(org_id="ORG-001", name="Acme")
    user = service.create_user(
        org_id="ORG-001",
        email="user@example.com",
        display_name="User",
        password="secret-password",
        user_id="usr-1",
    )

    token = service.create_api_token(user_id=user.id, scopes=["audit:read", "tasks:write"])

    assert token.token.startswith("hs_api_")
    assert service.validate_api_token(token.token).scopes == ("audit:read", "tasks:write")
    with sqlite3.connect(tmp_path / "enterprise.sqlite") as con:
        stored = con.execute("SELECT token_hash FROM api_tokens WHERE id = ?", (token.id,)).fetchone()[0]
    assert token.token not in stored
    assert service.revoke_api_token(token.token) is True
    assert service.validate_api_token(token.token) is None


def test_api_tokens_can_be_listed_and_revoked_by_id_without_secret_material(tmp_path):
    service = _service(tmp_path)
    service.create_organization(org_id="ORG-001", name="Acme")
    service.create_organization(org_id="ORG-002", name="Other")
    user = service.create_user(
        org_id="ORG-001",
        email="user@example.com",
        display_name="User",
        password="secret-password",
        user_id="usr-1",
    )
    other_user = service.create_user(
        org_id="ORG-002",
        email="other@example.com",
        display_name="Other",
        password="secret-password",
        user_id="usr-2",
    )

    token = service.create_api_token(user_id=user.id, scopes=["audit:read"])
    service.create_api_token(user_id=other_user.id, scopes=["audit:read"])

    active_tokens = service.list_api_tokens(org_id="ORG-001")
    assert [item.id for item in active_tokens] == [token.id]
    assert active_tokens[0].token == ""
    assert service.revoke_api_token_by_id(token.id) is True
    assert service.validate_api_token(token.token) is None
    assert service.revoke_api_token_by_id(token.id) is False
    assert service.list_api_tokens(org_id="ORG-001") == []
    revoked_tokens = service.list_api_tokens(org_id="ORG-001", include_revoked=True)
    assert [item.id for item in revoked_tokens] == [token.id]
    assert revoked_tokens[0].revoked_at


def test_service_account_creation_requires_scope(tmp_path):
    service = _service(tmp_path)
    service.create_organization(org_id="ORG-001", name="Acme")

    with pytest.raises(ValueError, match="scope"):
        service.create_service_account(org_id="ORG-001", name="nightly-loop", scopes=[])

    account = service.create_service_account(
        org_id="ORG-001",
        name="nightly-loop",
        scopes=["scheduler:run"],
        service_account_id="svc-nightly",
    )
    assert account.id == "svc-nightly"
    assert account.scopes == ("scheduler:run",)
    assert account.status == "active"
