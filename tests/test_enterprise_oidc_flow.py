from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from orchestrator.enterprise.auth_providers import load_auth_providers
from orchestrator.enterprise.oidc_flow import build_oidc_authorization_start


def _oidc_provider():
    return load_auth_providers(
        [
            {
                "type": "oidc",
                "id": "entra",
                "display_name": "Microsoft Entra ID",
                "enabled": True,
                "issuer": "https://login.microsoftonline.com/tenant/v2.0",
                "client_id": "hashi-client",
                "authorization_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize",
                "token_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
                "jwks_uri": "https://login.microsoftonline.com/tenant/discovery/v2.0/keys",
            }
        ]
    )[1]


def test_build_oidc_authorization_start_uses_pkce_and_keeps_verifier_private():
    start = build_oidc_authorization_start(
        _oidc_provider(),
        redirect_uri="https://hashi.example.com/api/auth/oidc/entra/callback",
    )

    parsed = urlparse(start.authorization_url)
    query = parse_qs(parsed.query)
    public = start.public_payload()

    assert parsed.scheme == "https"
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["hashi-client"]
    assert query["scope"] == ["openid email profile"]
    assert query["state"] == [start.state]
    assert query["nonce"] == [start.nonce]
    assert query["code_challenge"] == [start.code_challenge]
    assert query["code_challenge_method"] == ["S256"]
    assert start.code_verifier
    assert start.code_verifier not in start.authorization_url
    assert "code_verifier" not in public
    assert "nonce" not in public
