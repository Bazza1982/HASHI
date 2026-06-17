from __future__ import annotations

import pytest

from orchestrator.enterprise.auth_providers import load_auth_providers
from orchestrator.enterprise.oidc_exchange import (
    build_oidc_token_exchange_request,
    map_oidc_claims,
)
from orchestrator.enterprise.oidc_flow import build_oidc_authorization_start


def _provider(client_secret: str = "super-secret"):
    return load_auth_providers(
        [
            {
                "type": "oidc",
                "id": "entra",
                "display_name": "Microsoft Entra ID",
                "enabled": True,
                "issuer": "https://login.microsoftonline.com/tenant/v2.0",
                "client_id": "hashi-client",
                "client_secret": client_secret,
                "authorization_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize",
                "token_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
                "jwks_uri": "https://login.microsoftonline.com/tenant/discovery/v2.0/keys",
            }
        ]
    )[1]


def test_token_exchange_request_keeps_code_verifier_and_secret_private():
    provider = _provider()
    flow = build_oidc_authorization_start(
        provider,
        redirect_uri="https://hashi.example.com/api/auth/oidc/entra/callback",
    )

    request = build_oidc_token_exchange_request(provider, flow, code="auth-code")
    payload = request.public_payload()

    assert request.body["code"] == "auth-code"
    assert request.body["code_verifier"] == flow.code_verifier
    assert request.body["client_secret"] == "super-secret"
    assert payload["uses_client_secret"] is True
    assert "client_id" in payload["body_fields"]
    assert payload["redacted_body_field_count"] == 3
    assert "auth-code" not in repr(payload)
    assert flow.code_verifier not in repr(payload)
    assert "super-secret" not in repr(payload)
    assert "code_verifier" not in repr(payload)


def test_token_exchange_request_supports_public_oidc_client():
    provider = _provider(client_secret="")
    flow = build_oidc_authorization_start(
        provider,
        redirect_uri="https://hashi.example.com/api/auth/oidc/entra/callback",
    )

    request = build_oidc_token_exchange_request(provider, flow, code="auth-code")

    assert "client_secret" not in request.body
    assert request.public_payload()["uses_client_secret"] is False


def test_map_oidc_claims_builds_external_identity():
    identity = map_oidc_claims(
        provider_id="entra",
        org_id="ORG-001",
        claims={
            "sub": "subject-123",
            "email": "Admin@Example.com",
            "name": "Admin User",
        },
    )

    assert identity.external_user_id == "oidc:entra:subject-123"
    assert identity.email == "admin@example.com"
    assert identity.display_name == "Admin User"
    assert identity.public_payload()["external_user_id"] == "oidc:entra:subject-123"
    assert "subject" not in identity.public_payload()


@pytest.mark.parametrize(
    ("claims", "message"),
    [
        ({"email": "admin@example.com"}, "sub claim is required"),
        ({"sub": "subject-123", "email": "not-email"}, "email claim must be an email address"),
    ],
)
def test_map_oidc_claims_rejects_incomplete_identity(claims, message):
    with pytest.raises(ValueError, match=message):
        map_oidc_claims(provider_id="entra", org_id="ORG-001", claims=claims)
