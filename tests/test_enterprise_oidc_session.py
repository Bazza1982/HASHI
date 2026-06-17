from __future__ import annotations

import sqlite3

import pytest

from orchestrator.enterprise.identity import EnterpriseRole, IdentityService
from orchestrator.enterprise.oidc_exchange import map_oidc_claims
from orchestrator.enterprise.oidc_session import complete_oidc_session


def _service(tmp_path) -> IdentityService:
    return IdentityService.from_path(tmp_path / "enterprise.sqlite")


def _mapped_identity(email: str = "Admin@Example.com"):
    return map_oidc_claims(
        provider_id="entra",
        org_id="ORG-001",
        claims={"sub": "subject-123", "email": email, "name": "Admin User"},
    )


def test_complete_oidc_session_creates_individual_user_and_session(tmp_path):
    service = _service(tmp_path)
    service.create_organization(org_id="ORG-001", name="Acme")
    project = service.create_project(org_id="ORG-001", name="Default", project_id="ORG-001-default")

    completion = complete_oidc_session(
        identity_service=service,
        mapped_identity=_mapped_identity(),
        default_project_id=project.id,
    )

    assert completion.user_created is True
    assert completion.user.email == "admin@example.com"
    assert completion.session.token.startswith("hs_sess_")
    assert service.get_session_user(completion.session.token) == completion.user
    assert service.list_project_memberships(user_id=completion.user.id)[0]["role"] == EnterpriseRole.INDIVIDUAL_USER.value
    assert completion.public_payload()["default_role"] == EnterpriseRole.INDIVIDUAL_USER.value


def test_complete_oidc_session_reuses_existing_user_without_password_auth(tmp_path):
    service = _service(tmp_path)
    service.create_organization(org_id="ORG-001", name="Acme")
    existing = service.create_user(
        org_id="ORG-001",
        email="admin@example.com",
        display_name="Existing Admin",
        password="local-secret",
        user_id="usr-existing",
    )

    completion = complete_oidc_session(identity_service=service, mapped_identity=_mapped_identity())

    assert completion.user_created is False
    assert completion.user == existing
    assert service.get_session_user(completion.session.token) == existing


def test_complete_oidc_session_rejects_inactive_existing_user(tmp_path):
    service = _service(tmp_path)
    service.create_organization(org_id="ORG-001", name="Acme")
    service.create_user(
        org_id="ORG-001",
        email="admin@example.com",
        display_name="Inactive User",
        password="local-secret",
        status="disabled",
    )

    with pytest.raises(ValueError, match="OIDC user is not active"):
        complete_oidc_session(identity_service=service, mapped_identity=_mapped_identity())


def test_oidc_created_user_password_is_random_and_not_oidc_claim_material(tmp_path):
    service = _service(tmp_path)
    service.create_organization(org_id="ORG-001", name="Acme")

    completion = complete_oidc_session(identity_service=service, mapped_identity=_mapped_identity())

    with sqlite3.connect(tmp_path / "enterprise.sqlite") as con:
        stored = con.execute("SELECT password_hash FROM users WHERE id = ?", (completion.user.id,)).fetchone()[0]

    assert "subject-123" not in stored
    assert "admin@example.com" not in stored
    assert stored.startswith("pbkdf2_sha256$")
