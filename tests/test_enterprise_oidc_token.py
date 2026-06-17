from __future__ import annotations

from datetime import datetime, timezone

import pytest

from orchestrator.enterprise.auth_providers import load_auth_providers
from orchestrator.enterprise.oidc_flow import build_oidc_authorization_start
from orchestrator.enterprise.oidc_token import validate_oidc_id_token_claims


def _provider():
    return load_auth_providers(
        [
            {
                "type": "oidc",
                "id": "entra",
                "enabled": True,
                "issuer": "https://login.microsoftonline.com/tenant/v2.0",
                "client_id": "hashi-client",
                "authorization_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize",
                "token_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
                "jwks_uri": "https://login.microsoftonline.com/tenant/discovery/v2.0/keys",
            }
        ]
    )[1]


def _flow(provider):
    return build_oidc_authorization_start(
        provider,
        redirect_uri="https://hashi.example.com/api/auth/oidc/entra/callback",
    )


def _claims(flow, **overrides):
    claims = {
        "iss": "https://login.microsoftonline.com/tenant/v2.0",
        "sub": "subject-123",
        "aud": "hashi-client",
        "exp": 1_800_000_000,
        "iat": 1_700_000_000,
        "nonce": flow.nonce,
        "email": "Admin@Example.com",
    }
    claims.update(overrides)
    return claims


def test_validate_oidc_id_token_claims_accepts_core_claims():
    provider = _provider()
    flow = _flow(provider)

    validated = validate_oidc_id_token_claims(
        provider,
        flow,
        _claims(flow),
        now=datetime.fromtimestamp(1_700_000_100, tz=timezone.utc),
    )

    assert validated.provider_id == "entra"
    assert validated.subject == "subject-123"
    assert validated.email == "admin@example.com"
    assert validated.audience == ("hashi-client",)
    assert "nonce" not in validated.public_payload()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"iss": "https://evil.example.com"}, "issuer mismatch"),
        ({"aud": "other-client"}, "audience mismatch"),
        ({"exp": 1_600_000_000}, "ID token is expired"),
        ({"nbf": 1_900_000_000}, "ID token is not yet valid"),
        ({"iat": 1_900_000_000}, "ID token issued_at is in the future"),
        ({"nonce": "wrong"}, "nonce mismatch"),
        ({"sub": ""}, "sub claim is required"),
    ],
)
def test_validate_oidc_id_token_claims_rejects_unsafe_claims(override, message):
    provider = _provider()
    flow = _flow(provider)

    with pytest.raises(ValueError, match=message):
        validate_oidc_id_token_claims(
            provider,
            flow,
            _claims(flow, **override),
            now=datetime.fromtimestamp(1_700_000_100, tz=timezone.utc),
        )
