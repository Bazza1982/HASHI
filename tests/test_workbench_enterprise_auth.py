from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from orchestrator.enterprise import EnterpriseRole
from orchestrator.workbench_api import WorkbenchApiServer


SAML_METADATA = """\
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
    xmlns:ds="http://www.w3.org/2000/09/xmldsig#"
    entityID="https://idp.example.com/metadata">
  <md:IDPSSODescriptor>
    <md:KeyDescriptor use="signing">
      <ds:KeyInfo><ds:X509Data><ds:X509Certificate>MIIC FAKE CERT</ds:X509Certificate></ds:X509Data></ds:KeyInfo>
    </md:KeyDescriptor>
    <md:SingleSignOnService
      Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
      Location="https://idp.example.com/sso/redirect"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>
"""


SAML_ASSERTION = """\
<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">
  <saml:Issuer>https://idp.example.com/metadata</saml:Issuer>
  <saml:Subject><saml:NameID>saml@example.com</saml:NameID></saml:Subject>
  <saml:Conditions NotBefore="2026-06-17T00:00:00Z" NotOnOrAfter="2099-06-17T01:00:00Z">
    <saml:AudienceRestriction><saml:Audience>hashi-enterprise</saml:Audience></saml:AudienceRestriction>
  </saml:Conditions>
  <saml:AttributeStatement>
    <saml:Attribute Name="email"><saml:AttributeValue>saml@example.com</saml:AttributeValue></saml:Attribute>
    <saml:Attribute Name="displayName"><saml:AttributeValue>SAML User</saml:AttributeValue></saml:Attribute>
  </saml:AttributeStatement>
</saml:Assertion>
"""


class _FakeRequest:
    def __init__(
        self,
        payload: dict | None = None,
        *,
        headers: dict | None = None,
        path: str = "",
        query: dict | None = None,
        match_info: dict | None = None,
    ):
        self._payload = payload or {}
        self.headers = headers or {}
        self.path = path
        self.query = query or {}
        self.match_info = match_info or {}

    async def json(self):
        return self._payload


def _server(tmp_path: Path, *, profile: str = "enterprise", agents: list[dict] | None = None) -> WorkbenchApiServer:
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {"deployment_profile": profile, "organization_id": "ORG-001"},
                "agents": agents or [],
            }
        ),
        encoding="utf-8",
    )
    global_config = SimpleNamespace(
        deployment_profile=profile,
        organization_id="ORG-001",
        bridge_home=tmp_path,
        workbench_port=18800,
        project_root=tmp_path,
    )
    return WorkbenchApiServer(config_path=config_path, global_config=global_config)


def _audit_events(tmp_path: Path) -> list[dict]:
    path = tmp_path / "state" / "enterprise_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_enterprise_login_me_and_logout_flow(tmp_path):
    server = _server(tmp_path)
    server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )

    login_response = await server.handle_auth_login(
        _FakeRequest({"email": "admin@example.com", "password": "secret-password"})
    )
    login_payload = json.loads(login_response.text)
    token = login_payload["session"]["token"]

    assert login_response.status == 200
    assert login_payload["ok"] is True
    assert login_payload["user"]["memberships"][0]["role"] == EnterpriseRole.ORG_ADMIN.value
    assert _audit_events(tmp_path)[-1]["action"] == "login"

    me_response = await server.handle_auth_me(_FakeRequest(headers={"Authorization": f"Bearer {token}"}))
    me_payload = json.loads(me_response.text)
    assert me_response.status == 200
    assert me_payload["user"]["email"] == "admin@example.com"

    logout_response = await server.handle_auth_logout(_FakeRequest(headers={"Authorization": f"Bearer {token}"}))
    assert json.loads(logout_response.text)["revoked"] is True
    assert _audit_events(tmp_path)[-1]["action"] == "logout"

    me_after_logout = await server.handle_auth_me(_FakeRequest(headers={"Authorization": f"Bearer {token}"}))
    assert me_after_logout.status == 401


@pytest.mark.asyncio
async def test_enterprise_login_rejects_bad_credentials(tmp_path):
    server = _server(tmp_path)
    server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
    )

    response = await server.handle_auth_login(
        _FakeRequest({"email": "admin@example.com", "password": "wrong"})
    )

    assert response.status == 401
    assert json.loads(response.text)["ok"] is False
    event = _audit_events(tmp_path)[-1]
    assert event["action"] == "login"
    assert event["status"] == "failed"
    assert "wrong" not in json.dumps(event)


@pytest.mark.asyncio
async def test_auth_providers_exposes_local_and_oidc_metadata_without_secret(tmp_path):
    server = _server(tmp_path)
    server.global_config.enterprise_auth_providers = [
        {
            "type": "oidc",
            "id": "entra",
            "display_name": "Microsoft Entra ID",
            "enabled": True,
            "issuer": "https://login.microsoftonline.com/tenant/v2.0",
            "client_id": "hashi-client",
            "client_secret": "do-not-return",
            "authorization_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize",
            "token_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
            "jwks_uri": "https://login.microsoftonline.com/tenant/discovery/v2.0/keys",
        }
    ]

    response = await server.handle_auth_providers(_FakeRequest())
    payload = json.loads(response.text)

    assert response.status == 200
    assert [provider["id"] for provider in payload["providers"]] == ["local", "entra"]
    assert payload["providers"][1]["ready"] is True
    assert payload["providers"][1]["client_id"] == "hashi-client"
    assert "client_secret" not in json.dumps(payload)
    assert "do-not-return" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_oidc_start_returns_authorization_url_and_stores_private_flow(tmp_path):
    server = _server(tmp_path)
    server.global_config.enterprise_auth_providers = [
        {
            "type": "oidc",
            "id": "entra",
            "display_name": "Microsoft Entra ID",
            "enabled": True,
            "issuer": "https://login.microsoftonline.com/tenant/v2.0",
            "client_id": "hashi-client",
            "client_secret": "do-not-return",
            "authorization_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize",
            "token_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
            "jwks_uri": "https://login.microsoftonline.com/tenant/discovery/v2.0/keys",
        }
    ]

    response = await server.handle_auth_oidc_start(
        _FakeRequest(
            query={"redirect_uri": "https://hashi.example.com/api/auth/oidc/entra/callback"},
            match_info={"provider_id": "entra"},
        )
    )
    payload = json.loads(response.text)
    state = payload["oidc"]["state"]
    stored = server._pending_oidc_flows[state]

    assert response.status == 200
    assert payload["oidc"]["provider_id"] == "entra"
    assert "code_challenge=" in payload["oidc"]["authorization_url"]
    assert "code_verifier" not in json.dumps(payload)
    query = parse_qs(urlparse(payload["oidc"]["authorization_url"]).query)
    assert query["nonce"] == [stored.nonce]
    assert stored.code_verifier
    assert stored.nonce
    assert _audit_events(tmp_path)[-1]["action"] == "oidc_start"


@pytest.mark.asyncio
async def test_oidc_start_rejects_unready_provider(tmp_path):
    server = _server(tmp_path)
    server.global_config.enterprise_auth_providers = [
        {
            "type": "oidc",
            "id": "broken",
            "enabled": True,
            "issuer": "https://issuer.example.com",
            "client_id": "hashi-client",
        }
    ]

    response = await server.handle_auth_oidc_start(
        _FakeRequest(
            query={"redirect_uri": "https://hashi.example.com/callback"},
            match_info={"provider_id": "broken"},
        )
    )

    assert response.status == 400
    assert json.loads(response.text)["error"] == "OIDC provider is not ready"
    assert _audit_events(tmp_path)[-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_oidc_callback_validates_state_and_consumes_pending_flow(tmp_path):
    server = _server(tmp_path)
    server.global_config.enterprise_auth_providers = [
        {
            "type": "oidc",
            "id": "entra",
            "enabled": True,
            "issuer": "https://login.microsoftonline.com/tenant/v2.0",
            "client_id": "hashi-client",
            "client_secret": "do-not-return",
            "authorization_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize",
            "token_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
            "jwks_uri": "https://login.microsoftonline.com/tenant/discovery/v2.0/keys",
        }
    ]
    start_response = await server.handle_auth_oidc_start(
        _FakeRequest(
            query={"redirect_uri": "https://hashi.example.com/api/auth/oidc/entra/callback"},
            match_info={"provider_id": "entra"},
        )
    )
    state = json.loads(start_response.text)["oidc"]["state"]

    response = await server.handle_auth_oidc_callback(
        _FakeRequest(query={"state": state, "code": "auth-code"}, match_info={"provider_id": "entra"})
    )
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["oidc"]["code_received"] is True
    assert payload["oidc"]["token_exchange"] == "prepared"
    exchange = payload["oidc"]["token_exchange_request"]
    assert exchange["provider_id"] == "entra"
    assert exchange["uses_client_secret"] is True
    assert exchange["token_endpoint"] == "https://login.microsoftonline.com/tenant/oauth2/v2.0/token"
    assert "code" not in exchange["body_fields"]
    assert "code_verifier" not in exchange["body_fields"]
    assert "client_secret" not in exchange["body_fields"]
    assert state not in server._pending_oidc_flows
    response_text = json.dumps(payload)
    events_text = json.dumps(_audit_events(tmp_path))
    assert "auth-code" not in response_text
    assert "do-not-return" not in response_text
    assert "auth-code" not in events_text
    assert "do-not-return" not in events_text
    event = _audit_events(tmp_path)[-1]
    assert event["status"] == "validated"
    assert event["context"]["token_exchange"] == "prepared"


@pytest.mark.asyncio
async def test_oidc_callback_can_complete_verified_login_flow(tmp_path, monkeypatch):
    server = _server(tmp_path)
    server.global_config.enterprise_oidc_complete_login = True
    server.global_config.enterprise_auth_providers = [
        {
            "type": "oidc",
            "id": "entra",
            "enabled": True,
            "issuer": "https://login.microsoftonline.com/tenant/v2.0",
            "client_id": "hashi-client",
            "client_secret": "do-not-return",
            "authorization_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize",
            "token_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
            "jwks_uri": "https://login.microsoftonline.com/tenant/discovery/v2.0/keys",
        }
    ]
    server.identity_service.create_organization(org_id="ORG-001", name="Acme")
    server.identity_service.create_project(org_id="ORG-001", name="Default", project_id="ORG-001-default")

    def token_transport(_url, _body, _headers, _timeout):
        return 200, {
            "id_token": "id.jwt.token",
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "token_type": "Bearer",
            "expires_in": 3600,
        }

    def jwks_transport(_url, _body, _headers, _timeout):
        return 200, {"keys": [{"kid": "key-1", "kty": "RSA"}]}

    def fake_verify(_provider, flow, id_token, jwks):
        assert id_token == "id.jwt.token"
        assert jwks["keys"][0]["kid"] == "key-1"
        return SimpleNamespace(
            claims={
                "sub": "subject-123",
                "email": "Admin@Example.com",
                "name": "Admin User",
                "nonce": flow.nonce,
            }
        )

    server._oidc_token_transport = token_transport
    server._oidc_jwks_transport = jwks_transport
    monkeypatch.setattr("orchestrator.workbench_api.verify_oidc_id_token", fake_verify)

    start_response = await server.handle_auth_oidc_start(
        _FakeRequest(
            query={"redirect_uri": "https://hashi.example.com/api/auth/oidc/entra/callback"},
            match_info={"provider_id": "entra"},
        )
    )
    state = json.loads(start_response.text)["oidc"]["state"]

    response = await server.handle_auth_oidc_callback(
        _FakeRequest(query={"state": state, "code": "auth-code"}, match_info={"provider_id": "entra"})
    )
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["oidc"]["token_exchange"] == "completed"
    assert payload["user"]["email"] == "admin@example.com"
    assert payload["session"]["token"].startswith("hs_sess_")
    assert server.identity_service.get_session_user(payload["session"]["token"]).email == "admin@example.com"
    memberships = server.identity_service.list_project_memberships(user_id=payload["user"]["id"])
    assert memberships[0]["project_id"] == "ORG-001-default"
    assert memberships[0]["role"] == EnterpriseRole.INDIVIDUAL_USER.value
    response_text = json.dumps(payload)
    events_text = json.dumps(_audit_events(tmp_path))
    assert "id.jwt.token" not in response_text
    assert "access-secret" not in response_text
    assert "refresh-secret" not in response_text
    assert "auth-code" not in events_text
    assert "id.jwt.token" not in events_text
    assert _audit_events(tmp_path)[-1]["context"]["token_exchange"] == "completed"


@pytest.mark.asyncio
async def test_oidc_callback_rejects_invalid_state(tmp_path):
    server = _server(tmp_path)

    response = await server.handle_auth_oidc_callback(
        _FakeRequest(query={"state": "missing", "code": "auth-code"}, match_info={"provider_id": "entra"})
    )

    assert response.status == 400
    assert json.loads(response.text)["error"] == "invalid OIDC state"
    assert _audit_events(tmp_path)[-1]["context"]["error"] == "invalid state"


@pytest.mark.asyncio
async def test_oidc_callback_reports_provider_error(tmp_path):
    server = _server(tmp_path)

    response = await server.handle_auth_oidc_callback(
        _FakeRequest(
            query={"error": "access_denied", "error_description": "User denied"},
            match_info={"provider_id": "entra"},
        )
    )

    assert response.status == 400
    assert json.loads(response.text)["error"] == "access_denied"
    event = _audit_events(tmp_path)[-1]
    assert event["status"] == "failed"
    assert event["context"]["error"] == "access_denied"


@pytest.mark.asyncio
async def test_saml_start_returns_authn_request_and_keeps_metadata_private(tmp_path):
    server = _server(tmp_path)
    server.global_config.enterprise_auth_providers = [
        {
            "type": "saml",
            "id": "okta-saml",
            "enabled": True,
            "metadata_xml": SAML_METADATA,
            "sp_entity_id": "hashi-enterprise",
            "acs_url": "https://hashi.example.com/api/auth/saml/okta-saml/callback",
        }
    ]

    response = await server.handle_auth_saml_start(_FakeRequest(match_info={"provider_id": "okta-saml"}))
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["saml"]["provider_id"] == "okta-saml"
    assert payload["saml"]["state"].startswith("saml_")
    assert payload["saml"]["request_id"].startswith("_")
    assert "SAMLRequest=" in payload["saml"]["redirect_url"]
    assert payload["saml"]["state"] in server._pending_saml_flows
    assert "MIIC" not in json.dumps(payload)
    assert _audit_events(tmp_path)[-1]["action"] == "saml_start"


@pytest.mark.asyncio
async def test_saml_callback_can_complete_preverified_login_flow(tmp_path):
    server = _server(tmp_path)
    server.global_config.enterprise_saml_allow_preverified_assertions = True
    server.global_config.enterprise_auth_providers = [
        {
            "type": "saml",
            "id": "okta-saml",
            "enabled": True,
            "metadata_xml": SAML_METADATA,
            "sp_entity_id": "hashi-enterprise",
            "acs_url": "https://hashi.example.com/api/auth/saml/okta-saml/callback",
        }
    ]
    server.identity_service.create_organization(org_id="ORG-001", name="Acme")
    server.identity_service.create_project(org_id="ORG-001", name="Default", project_id="ORG-001-default")
    start_response = await server.handle_auth_saml_start(_FakeRequest(match_info={"provider_id": "okta-saml"}))
    state = json.loads(start_response.text)["saml"]["state"]
    encoded_assertion = base64.b64encode(SAML_ASSERTION.encode("utf-8")).decode("ascii")

    response = await server.handle_auth_saml_callback(
        _FakeRequest(
            {"RelayState": state, "SAMLResponse": encoded_assertion, "signature_verified": True},
            match_info={"provider_id": "okta-saml"},
        )
    )
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["saml"]["signature_verified"] is True
    assert payload["user"]["email"] == "saml@example.com"
    assert payload["session"]["token"].startswith("hs_sess_")
    assert server.identity_service.get_session_user(payload["session"]["token"]).email == "saml@example.com"
    memberships = server.identity_service.list_project_memberships(user_id=payload["user"]["id"])
    assert memberships[0]["project_id"] == "ORG-001-default"
    events_text = json.dumps(_audit_events(tmp_path))
    assert "SAMLResponse" not in events_text
    assert "saml@example.com" not in events_text
    assert state not in server._pending_saml_flows


@pytest.mark.asyncio
async def test_saml_callback_uses_builtin_signature_verifier_by_default(tmp_path, monkeypatch):
    server = _server(tmp_path)
    server.global_config.enterprise_auth_providers = [
        {
            "type": "saml",
            "id": "okta-saml",
            "enabled": True,
            "metadata_xml": SAML_METADATA,
            "sp_entity_id": "hashi-enterprise",
            "acs_url": "https://hashi.example.com/api/auth/saml/okta-saml/callback",
        }
    ]
    server.identity_service.create_organization(org_id="ORG-001", name="Acme")
    server.identity_service.create_project(org_id="ORG-001", name="Default", project_id="ORG-001-default")
    start_response = await server.handle_auth_saml_start(_FakeRequest(match_info={"provider_id": "okta-saml"}))
    state = json.loads(start_response.text)["saml"]["state"]
    encoded_assertion = base64.b64encode(SAML_ASSERTION.encode("utf-8")).decode("ascii")
    calls = []

    def fake_verify(assertion_xml, provider):
        calls.append((provider.id, assertion_xml))
        return True

    monkeypatch.setattr("orchestrator.workbench_api.verify_saml_assertion_signature", fake_verify)

    response = await server.handle_auth_saml_callback(
        _FakeRequest(
            {"RelayState": state, "SAMLResponse": encoded_assertion},
            match_info={"provider_id": "okta-saml"},
        )
    )
    payload = json.loads(response.text)

    assert response.status == 200
    assert calls[0][0] == "okta-saml"
    assert payload["saml"]["signature_verified"] is True
    assert payload["user"]["email"] == "saml@example.com"


@pytest.mark.asyncio
async def test_saml_callback_rejects_unverified_assertion(tmp_path):
    server = _server(tmp_path)
    server.global_config.enterprise_auth_providers = [
        {
            "type": "saml",
            "id": "okta-saml",
            "enabled": True,
            "metadata_xml": SAML_METADATA,
            "sp_entity_id": "hashi-enterprise",
            "acs_url": "https://hashi.example.com/api/auth/saml/okta-saml/callback",
        }
    ]
    start_response = await server.handle_auth_saml_start(_FakeRequest(match_info={"provider_id": "okta-saml"}))
    state = json.loads(start_response.text)["saml"]["state"]

    response = await server.handle_auth_saml_callback(
        _FakeRequest(
            {"RelayState": state, "assertion_xml": SAML_ASSERTION, "signature_verified": True},
            match_info={"provider_id": "okta-saml"},
        )
    )

    assert response.status == 400
    assert "XML Signature is required" in json.loads(response.text)["error"]
    assert _audit_events(tmp_path)[-1]["action"] == "saml_callback"


def test_enterprise_admin_auth_requires_admin_project_role(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    user = server.identity_service.create_user(
        org_id="ORG-001",
        email="user@example.com",
        display_name="User",
        password="secret-password",
        user_id="usr-user",
    )
    server.identity_service.assign_project_role(
        user_id=user.id,
        project_id="ORG-001-default",
        role=EnterpriseRole.INDIVIDUAL_USER,
    )
    admin_session = server.identity_service.create_session(user_id=admin.id)
    user_session = server.identity_service.create_session(user_id=user.id)

    assert server._check_admin_auth(_FakeRequest(headers={"Authorization": f"Bearer {admin_session.token}"}))
    assert not server._check_admin_auth(_FakeRequest(headers={"Authorization": f"Bearer {user_session.token}"}))
    assert not server._check_admin_auth(_FakeRequest(headers={}))


@pytest.mark.asyncio
async def test_enterprise_admin_can_create_and_list_projects(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    session = server.identity_service.create_session(user_id=admin.id)
    headers = {"Authorization": f"Bearer {session.token}"}

    create_response = await server.handle_enterprise_projects_create(
        _FakeRequest(
            {
                "name": "Research",
                "workspace_root": "/srv/hashi/research",
                "project_id": "prj-research",
            },
            headers=headers,
        )
    )
    list_response = await server.handle_enterprise_projects(_FakeRequest(headers=headers))

    assert create_response.status == 201
    projects = json.loads(list_response.text)["projects"]
    assert [project["id"] for project in projects] == ["ORG-001-default", "prj-research"]
    event = _audit_events(tmp_path)[-1]
    assert event["action"] == "project_create"
    assert event["context"]["project_id"] == "prj-research"


@pytest.mark.asyncio
async def test_enterprise_admin_can_create_and_list_users(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    project = server.identity_service.create_project(
        org_id="ORG-001",
        name="Research",
        project_id="prj-research",
    )
    session = server.identity_service.create_session(user_id=admin.id)
    headers = {"Authorization": f"Bearer {session.token}"}

    create_response = await server.handle_enterprise_users_create(
        _FakeRequest(
            {
                "email": "user@example.com",
                "display_name": "User",
                "password": "secret-password",
                "user_id": "usr-user",
                "project_id": project.id,
                "role": EnterpriseRole.INDIVIDUAL_USER.value,
            },
            headers=headers,
        )
    )
    list_response = await server.handle_enterprise_users(_FakeRequest(headers=headers))

    assert create_response.status == 201
    users = json.loads(list_response.text)["users"]
    assert [user["email"] for user in users] == ["admin@example.com", "user@example.com"]
    created = json.loads(create_response.text)["user"]
    assert created["memberships"][0]["role"] == EnterpriseRole.INDIVIDUAL_USER.value
    event = _audit_events(tmp_path)[-1]
    assert event["action"] == "user_create"
    assert event["context"]["target_user_id"] == "usr-user"
    assert "secret-password" not in json.dumps(event)


@pytest.mark.asyncio
async def test_enterprise_admin_can_scim_upsert_user(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    project = server.identity_service.create_project(org_id="ORG-001", name="Research", project_id="prj-research")
    session = server.identity_service.create_session(user_id=admin.id)
    headers = {"Authorization": f"Bearer {session.token}"}

    create_response = await server.handle_enterprise_scim_users_upsert(
        _FakeRequest(
            {
                "default_project_id": project.id,
                "scim": {
                    "userName": "SCIM@Example.com",
                    "displayName": "SCIM User",
                    "externalId": "idp-123",
                    "active": True,
                },
            },
            headers=headers,
        )
    )
    update_response = await server.handle_enterprise_scim_users_upsert(
        _FakeRequest(
            {
                "scim": {
                    "userName": "scim@example.com",
                    "displayName": "Renamed User",
                    "externalId": "idp-123",
                    "active": True,
                },
            },
            headers=headers,
        )
    )

    assert create_response.status == 201
    assert update_response.status == 200
    created = json.loads(create_response.text)
    updated = json.loads(update_response.text)
    assert created["scim"]["created"] is True
    assert updated["scim"]["created"] is False
    assert updated["user"]["display_name"] == "Renamed User"
    assert created["user"]["memberships"][0]["project_id"] == "prj-research"
    event = _audit_events(tmp_path)[-1]
    assert event["action"] == "scim_user_upsert"
    assert event["context"]["external_id"] == "idp-123"


@pytest.mark.asyncio
async def test_enterprise_admin_can_scim_deactivate_user_and_revoke_tokens(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    user = server.identity_service.create_user(
        org_id="ORG-001",
        email="user@example.com",
        display_name="User",
        password="secret-password",
        user_id="usr-user",
    )
    user_session = server.identity_service.create_session(user_id=user.id)
    api_token = server.identity_service.create_api_token(user_id=user.id, scopes=["audit:read"])
    admin_session = server.identity_service.create_session(user_id=admin.id)
    headers = {"Authorization": f"Bearer {admin_session.token}"}

    response = await server.handle_enterprise_scim_users_deactivate(
        _FakeRequest({"userName": "user@example.com"}, headers=headers)
    )

    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["user"]["status"] == "disabled"
    assert server.identity_service.get_session_user(user_session.token) is None
    assert server.identity_service.validate_api_token(api_token.token) is None
    event = _audit_events(tmp_path)[-1]
    assert event["action"] == "scim_user_deactivate"
    assert event["context"]["target_user_id"] == "usr-user"


@pytest.mark.asyncio
async def test_enterprise_admin_can_use_scim_v2_users_surface(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    session = server.identity_service.create_session(user_id=admin.id)
    headers = {"Authorization": f"Bearer {session.token}"}

    create_response = await server.handle_enterprise_scim_v2_users_create(
        _FakeRequest(
            {
                "userName": "SCIM2@Example.com",
                "displayName": "SCIM Two",
                "active": True,
            },
            headers=headers,
        )
    )
    created = json.loads(create_response.text)
    list_response = await server.handle_enterprise_scim_v2_users_list(
        _FakeRequest(headers=headers, query={"filter": 'userName eq "scim2@example.com"'})
    )
    listed = json.loads(list_response.text)
    get_response = await server.handle_enterprise_scim_v2_users_get(
        _FakeRequest(headers=headers, match_info={"user_id": created["id"]})
    )

    assert create_response.status == 201
    assert created["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:User"]
    assert created["userName"] == "scim2@example.com"
    assert listed["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]
    assert listed["totalResults"] == 1
    assert listed["Resources"][0]["id"] == created["id"]
    assert json.loads(get_response.text)["displayName"] == "SCIM Two"
    assert _audit_events(tmp_path)[-1]["action"] == "scim_v2_user_create"


@pytest.mark.asyncio
async def test_enterprise_admin_can_use_scim_v2_groups_surface(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    project = server.identity_service.create_project(org_id="ORG-001", name="Research", project_id="prj-research")
    user = server.identity_service.create_user(
        org_id="ORG-001",
        email="user@example.com",
        display_name="User",
        password="secret-password",
        user_id="usr-user",
    )
    server.identity_service.assign_project_role(
        user_id=user.id,
        project_id=project.id,
        role=EnterpriseRole.INDIVIDUAL_USER,
    )
    session = server.identity_service.create_session(user_id=admin.id)
    headers = {"Authorization": f"Bearer {session.token}"}

    list_response = await server.handle_enterprise_scim_v2_groups_list(
        _FakeRequest(headers=headers, query={"filter": 'displayName eq "Research"'})
    )
    get_response = await server.handle_enterprise_scim_v2_groups_get(
        _FakeRequest(headers=headers, match_info={"group_id": project.id})
    )
    listed = json.loads(list_response.text)
    group = json.loads(get_response.text)

    assert list_response.status == 200
    assert listed["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]
    assert listed["totalResults"] == 1
    assert listed["Resources"][0]["id"] == "prj-research"
    assert group["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:Group"]
    assert group["members"][0]["value"] == "usr-user"


@pytest.mark.asyncio
async def test_enterprise_admin_can_use_scim_v2_discovery_surface(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    session = server.identity_service.create_session(user_id=admin.id)
    headers = {"Authorization": f"Bearer {session.token}"}

    config_response = await server.handle_enterprise_scim_v2_service_provider_config(_FakeRequest(headers=headers))
    resource_types_response = await server.handle_enterprise_scim_v2_resource_types(_FakeRequest(headers=headers))
    group_type_response = await server.handle_enterprise_scim_v2_resource_type_get(
        _FakeRequest(headers=headers, match_info={"resource_type": "Group"})
    )
    schemas_response = await server.handle_enterprise_scim_v2_schemas(_FakeRequest(headers=headers))
    user_schema_response = await server.handle_enterprise_scim_v2_schema_get(
        _FakeRequest(headers=headers, match_info={"schema_id": "urn:ietf:params:scim:schemas:core:2.0:User"})
    )

    assert config_response.status == 200
    assert json.loads(config_response.text)["filter"]["supported"] is True
    assert {item["id"] for item in json.loads(resource_types_response.text)["Resources"]} == {"User", "Group"}
    assert json.loads(group_type_response.text)["endpoint"] == "/Groups"
    assert json.loads(schemas_response.text)["totalResults"] == 2
    assert json.loads(user_schema_response.text)["name"] == "User"


@pytest.mark.asyncio
async def test_enterprise_admin_can_scim_v2_patch_user_and_revoke_tokens(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    user = server.identity_service.create_user(
        org_id="ORG-001",
        email="user@example.com",
        display_name="User",
        password="secret-password",
        user_id="usr-user",
    )
    api_token = server.identity_service.create_api_token(user_id=user.id, scopes=["audit:read"])
    session = server.identity_service.create_session(user_id=admin.id)
    headers = {"Authorization": f"Bearer {session.token}"}

    patch_response = await server.handle_enterprise_scim_v2_users_patch(
        _FakeRequest(
            {"Operations": [{"op": "replace", "path": "active", "value": False}]},
            headers=headers,
            match_info={"user_id": "usr-user"},
        )
    )

    assert patch_response.status == 200
    payload = json.loads(patch_response.text)
    assert payload["active"] is False
    assert server.identity_service.validate_api_token(api_token.token) is None
    event = _audit_events(tmp_path)[-1]
    assert event["action"] == "scim_v2_user_patch"
    assert event["context"]["provisioning_action"] == "deactivated"


@pytest.mark.asyncio
async def test_public_scim_v2_users_requires_scoped_api_token(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    session = server.identity_service.create_session(user_id=admin.id)
    unscoped = server.identity_service.create_api_token(user_id=admin.id, scopes=["audit:read"])

    session_response = await server.handle_public_scim_v2_users_list(
        _FakeRequest(headers={"Authorization": f"Bearer {session.token}"}, path="/scim/v2/Users")
    )
    unscoped_response = await server.handle_public_scim_v2_users_list(
        _FakeRequest(headers={"Authorization": f"Bearer {unscoped.token}"}, path="/scim/v2/Users")
    )

    assert session_response.status == 403
    assert unscoped_response.status == 403
    events = _audit_events(tmp_path)
    assert events[-1]["action"] == "scim_token_auth"
    assert events[-1]["status"] == "denied"
    assert unscoped.token not in json.dumps(events)


@pytest.mark.asyncio
async def test_public_scim_v2_users_supports_service_token_create_list_and_patch(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    scim_token = server.identity_service.create_api_token(user_id=admin.id, scopes=["scim:write"])
    headers = {"Authorization": f"Bearer {scim_token.token}"}

    create_response = await server.handle_public_scim_v2_users_create(
        _FakeRequest({"userName": "PublicSCIM@Example.com", "displayName": "Public SCIM"}, headers=headers)
    )
    created = json.loads(create_response.text)
    list_response = await server.handle_public_scim_v2_users_list(
        _FakeRequest(headers=headers, query={"filter": 'emails.value eq "publicscim@example.com"'})
    )
    patch_response = await server.handle_public_scim_v2_users_patch(
        _FakeRequest(
            {"Operations": [{"op": "replace", "path": "active", "value": False}]},
            headers=headers,
            match_info={"user_id": created["id"]},
        )
    )

    assert create_response.status == 201
    assert created["userName"] == "publicscim@example.com"
    assert json.loads(list_response.text)["totalResults"] == 1
    assert json.loads(patch_response.text)["active"] is False
    assert server.identity_service.get_user(created["id"]).status == "disabled"
    event = _audit_events(tmp_path)[-1]
    assert event["event_type"] == "scim"
    assert event["action"] == "scim_v2_user_patch"
    assert event["context"]["api_token_id"] == scim_token.id
    assert scim_token.token not in json.dumps(_audit_events(tmp_path))


@pytest.mark.asyncio
async def test_public_scim_v2_groups_supports_service_token_read(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    project = server.identity_service.create_project(org_id="ORG-001", name="Research", project_id="prj-research")
    user = server.identity_service.create_user(
        org_id="ORG-001",
        email="user@example.com",
        display_name="User",
        password="secret-password",
        user_id="usr-user",
    )
    server.identity_service.assign_project_role(
        user_id=user.id,
        project_id=project.id,
        role=EnterpriseRole.INDIVIDUAL_USER,
    )
    scim_token = server.identity_service.create_api_token(user_id=admin.id, scopes=["scim:read"])
    headers = {"Authorization": f"Bearer {scim_token.token}"}

    list_response = await server.handle_public_scim_v2_groups_list(
        _FakeRequest(headers=headers, query={"filter": 'id eq "prj-research"'}, path="/scim/v2/Groups")
    )
    get_response = await server.handle_public_scim_v2_groups_get(
        _FakeRequest(headers=headers, match_info={"group_id": project.id}, path=f"/scim/v2/Groups/{project.id}")
    )
    listed = json.loads(list_response.text)
    group = json.loads(get_response.text)

    assert list_response.status == 200
    assert get_response.status == 200
    assert listed["totalResults"] == 1
    assert group["displayName"] == "Research"
    assert group["members"][0]["value"] == "usr-user"


@pytest.mark.asyncio
async def test_public_scim_v2_discovery_requires_scoped_service_token(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    scim_token = server.identity_service.create_api_token(user_id=admin.id, scopes=["scim:read"])
    headers = {"Authorization": f"Bearer {scim_token.token}"}

    denied = await server.handle_public_scim_v2_service_provider_config(_FakeRequest(path="/scim/v2/ServiceProviderConfig"))
    config_response = await server.handle_public_scim_v2_service_provider_config(_FakeRequest(headers=headers))
    resource_type_response = await server.handle_public_scim_v2_resource_type_get(
        _FakeRequest(headers=headers, match_info={"resource_type": "User"})
    )
    schema_response = await server.handle_public_scim_v2_schema_get(
        _FakeRequest(headers=headers, match_info={"schema_id": "urn:ietf:params:scim:schemas:core:2.0:Group"})
    )

    assert denied.status == 403
    assert config_response.status == 200
    assert json.loads(config_response.text)["authenticationSchemes"][0]["type"] == "oauthbearertoken"
    assert json.loads(resource_type_response.text)["schema"] == "urn:ietf:params:scim:schemas:core:2.0:User"
    assert json.loads(schema_response.text)["name"] == "Group"


@pytest.mark.asyncio
async def test_public_scim_v2_bulk_supports_scoped_service_token(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    scim_token = server.identity_service.create_api_token(user_id=admin.id, scopes=["scim:write"])
    headers = {"Authorization": f"Bearer {scim_token.token}"}

    response = await server.handle_public_scim_v2_bulk(
        _FakeRequest(
            {
                "failOnErrors": 1,
                "Operations": [
                    {
                        "method": "POST",
                        "path": "/Users",
                        "bulkId": "user-1",
                        "data": {"userName": "bulk@example.com", "displayName": "Bulk User"},
                    }
                ],
            },
            headers=headers,
            path="/scim/v2/Bulk",
        )
    )
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:BulkResponse"]
    assert payload["Operations"][0]["status"] == "201"
    assert payload["Operations"][0]["bulkId"] == "user-1"
    assert server.identity_service.get_user_by_email(org_id="ORG-001", email="bulk@example.com") is not None
    event = _audit_events(tmp_path)[-1]
    assert event["action"] == "scim_v2_bulk"
    assert event["context"]["operation_count"] == 1
    assert scim_token.token not in json.dumps(_audit_events(tmp_path))


@pytest.mark.asyncio
async def test_public_scim_v2_rejects_cross_org_target_user(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    server.identity_service.create_organization(org_id="ORG-002", name="Other")
    other = server.identity_service.create_user(
        org_id="ORG-002",
        email="other@example.com",
        display_name="Other",
        password="secret-password",
        user_id="usr-other",
    )
    scim_token = server.identity_service.create_api_token(user_id=admin.id, scopes=["scim:read"])

    response = await server.handle_public_scim_v2_users_get(
        _FakeRequest(headers={"Authorization": f"Bearer {scim_token.token}"}, match_info={"user_id": other.id})
    )

    assert response.status == 404
    assert "usr-other" in json.loads(response.text)["detail"]


@pytest.mark.asyncio
async def test_enterprise_admin_can_create_list_and_revoke_api_tokens(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    user = server.identity_service.create_user(
        org_id="ORG-001",
        email="user@example.com",
        display_name="User",
        password="secret-password",
        user_id="usr-user",
    )
    session = server.identity_service.create_session(user_id=admin.id)
    headers = {"Authorization": f"Bearer {session.token}"}

    create_response = await server.handle_enterprise_api_tokens_create(
        _FakeRequest({"user_id": user.id, "scopes": ["audit:read", "tasks:write"]}, headers=headers)
    )
    create_payload = json.loads(create_response.text)
    api_token = create_payload["api_token"]

    assert create_response.status == 201
    assert api_token["token"].startswith("hs_api_")
    assert api_token["scopes"] == ["audit:read", "tasks:write"]
    assert server.identity_service.validate_api_token(api_token["token"]).id == api_token["id"]

    list_response = await server.handle_enterprise_api_tokens(_FakeRequest(headers=headers))
    listed = json.loads(list_response.text)["api_tokens"]
    assert [item["id"] for item in listed] == [api_token["id"]]
    assert "token" not in listed[0]
    assert "token_hash" not in json.dumps(listed)

    revoke_response = await server.handle_enterprise_api_token_revoke(
        _FakeRequest(headers=headers, match_info={"token_id": api_token["id"]})
    )
    assert json.loads(revoke_response.text)["revoked"] is True
    assert server.identity_service.validate_api_token(api_token["token"]) is None

    active_after_revoke = await server.handle_enterprise_api_tokens(_FakeRequest(headers=headers))
    assert json.loads(active_after_revoke.text)["count"] == 0
    revoked_list = await server.handle_enterprise_api_tokens(
        _FakeRequest(headers=headers, query={"include_revoked": "true"})
    )
    assert json.loads(revoked_list.text)["count"] == 1

    events = _audit_events(tmp_path)
    assert [event["action"] for event in events[-2:]] == ["api_token_create", "api_token_revoke"]
    assert api_token["token"] not in json.dumps(events)


@pytest.mark.asyncio
async def test_enterprise_api_token_create_rejects_cross_org_user(tmp_path):
    server = _server(tmp_path)
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    server.identity_service.create_organization(org_id="ORG-002", name="Other")
    other_user = server.identity_service.create_user(
        org_id="ORG-002",
        email="other@example.com",
        display_name="Other",
        password="secret-password",
        user_id="usr-other",
    )
    session = server.identity_service.create_session(user_id=admin.id)

    response = await server.handle_enterprise_api_tokens_create(
        _FakeRequest(
            {"user_id": other_user.id, "scopes": ["audit:read"]},
            headers={"Authorization": f"Bearer {session.token}"},
        )
    )

    assert response.status == 400
    assert "not in this organization" in json.loads(response.text)["error"]
    event = _audit_events(tmp_path)[-1]
    assert event["action"] == "api_token_create"
    assert event["status"] == "failed"


@pytest.mark.asyncio
async def test_enterprise_admin_api_rejects_individual_user(tmp_path):
    server = _server(tmp_path)
    server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
    )
    user = server.identity_service.create_user(
        org_id="ORG-001",
        email="user@example.com",
        display_name="User",
        password="secret-password",
        user_id="usr-user",
    )
    server.identity_service.assign_project_role(
        user_id=user.id,
        project_id="ORG-001-default",
        role=EnterpriseRole.INDIVIDUAL_USER,
    )
    session = server.identity_service.create_session(user_id=user.id)

    response = await server.handle_enterprise_users(
        _FakeRequest(headers={"Authorization": f"Bearer {session.token}"}, path="/api/enterprise/users")
    )

    assert response.status == 403
    assert json.loads(response.text)["error"] == "admin auth failed"
    event = _audit_events(tmp_path)[-1]
    assert event["action"] == "admin_auth"
    assert event["status"] == "denied"
    assert event["context"]["path"] == "/api/enterprise/users"


def test_personal_profile_keeps_legacy_workbench_token(tmp_path):
    config_path = tmp_path / "agents.json"
    config_path.write_text(json.dumps({"global": {}, "agents": []}), encoding="utf-8")
    global_config = SimpleNamespace(
        deployment_profile="personal",
        bridge_home=tmp_path,
        workbench_port=18800,
        project_root=tmp_path,
    )
    server = WorkbenchApiServer(
        config_path=config_path,
        global_config=global_config,
        secrets={"workbench_admin_token": "legacy-secret"},
    )

    assert server._check_admin_auth(_FakeRequest(headers={"X-Workbench-Token": "legacy-secret"}))
    assert not server._check_admin_auth(_FakeRequest(headers={"X-Workbench-Token": "wrong"}))


@pytest.mark.asyncio
async def test_personal_profile_lists_all_agents_without_session(tmp_path):
    server = _server(
        tmp_path,
        profile="personal",
        agents=[
            {
                "name": "zelda",
                "display_name": "Zelda",
                "workspace_dir": "workspaces/zelda",
                "type": "flex",
            },
            {
                "name": "nana",
                "display_name": "Nana",
                "workspace_dir": "workspaces/nana",
                "type": "flex",
                "project_id": "prj-research",
            },
        ],
    )

    response = await server.handle_agents(_FakeRequest())

    assert response.status == 200
    payload = json.loads(response.text)
    assert [agent["name"] for agent in payload["agents"]] == ["zelda", "nana"]


@pytest.mark.asyncio
async def test_enterprise_agents_requires_session(tmp_path):
    server = _server(
        tmp_path,
        agents=[
            {
                "name": "nana",
                "display_name": "Nana",
                "workspace_dir": "workspaces/nana",
                "type": "flex",
                "project_id": "prj-research",
            },
        ],
    )

    response = await server.handle_agents(_FakeRequest(path="/api/agents"))

    assert response.status == 401
    assert json.loads(response.text)["error"] == "not authenticated"


@pytest.mark.asyncio
async def test_enterprise_admin_can_list_all_agents(tmp_path):
    server = _server(
        tmp_path,
        agents=[
            {
                "name": "unscoped",
                "display_name": "Unscoped",
                "workspace_dir": "workspaces/unscoped",
                "type": "flex",
            },
            {
                "name": "research",
                "display_name": "Research",
                "workspace_dir": "workspaces/research",
                "type": "flex",
                "project_id": "prj-research",
            },
        ],
    )
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    session = server.identity_service.create_session(user_id=admin.id)

    response = await server.handle_agents(_FakeRequest(headers={"Authorization": f"Bearer {session.token}"}))

    assert response.status == 200
    assert [agent["name"] for agent in json.loads(response.text)["agents"]] == ["unscoped", "research"]


@pytest.mark.asyncio
async def test_enterprise_individual_user_sees_only_project_agents(tmp_path):
    server = _server(
        tmp_path,
        agents=[
            {
                "name": "unscoped",
                "display_name": "Unscoped",
                "workspace_dir": "workspaces/unscoped",
                "type": "flex",
            },
            {
                "name": "research",
                "display_name": "Research",
                "workspace_dir": "workspaces/research",
                "type": "flex",
                "project_id": "prj-research",
            },
            {
                "name": "finance",
                "display_name": "Finance",
                "workspace_dir": "workspaces/finance",
                "type": "flex",
                "project_ids": ["prj-finance"],
            },
            {
                "name": "shared",
                "display_name": "Shared",
                "workspace_dir": "workspaces/shared",
                "type": "flex",
                "project_ids": ["prj-research", "prj-ops"],
            },
        ],
    )
    server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
    )
    server.identity_service.create_project(org_id="ORG-001", name="Research", project_id="prj-research")
    user = server.identity_service.create_user(
        org_id="ORG-001",
        email="user@example.com",
        display_name="User",
        password="secret-password",
        user_id="usr-user",
    )
    server.identity_service.assign_project_role(
        user_id=user.id,
        project_id="prj-research",
        role=EnterpriseRole.INDIVIDUAL_USER,
    )
    session = server.identity_service.create_session(user_id=user.id)

    response = await server.handle_agents(_FakeRequest(headers={"Authorization": f"Bearer {session.token}"}))

    assert response.status == 200
    assert [agent["name"] for agent in json.loads(response.text)["agents"]] == ["research", "shared"]
