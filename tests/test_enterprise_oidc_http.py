from __future__ import annotations

import pytest

from orchestrator.enterprise.auth_providers import load_auth_providers
from orchestrator.enterprise.oidc_exchange import build_oidc_token_exchange_request
from orchestrator.enterprise.oidc_flow import build_oidc_authorization_start
from orchestrator.enterprise.oidc_http import (
    OidcJwksCache,
    exchange_oidc_authorization_code,
    fetch_oidc_jwks,
)


def _provider():
    return load_auth_providers(
        [
            {
                "type": "oidc",
                "id": "entra",
                "enabled": True,
                "issuer": "https://login.microsoftonline.com/tenant/v2.0",
                "client_id": "hashi-client",
                "client_secret": "secret",
                "authorization_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize",
                "token_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
                "jwks_uri": "https://login.microsoftonline.com/tenant/discovery/v2.0/keys",
            }
        ]
    )[1]


def _exchange_request():
    provider = _provider()
    flow = build_oidc_authorization_start(
        provider,
        redirect_uri="https://hashi.example.com/api/auth/oidc/entra/callback",
    )
    return build_oidc_token_exchange_request(provider, flow, code="auth-code")


def test_exchange_oidc_authorization_code_returns_private_token_response():
    seen = {}

    def transport(url, body, headers, timeout):
        seen.update({"url": url, "body": body, "headers": headers, "timeout": timeout})
        return 200, {
            "id_token": "id.jwt.token",
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "token_type": "Bearer",
            "expires_in": "3600",
            "scope": "openid email",
        }

    response = exchange_oidc_authorization_code(_exchange_request(), transport=transport, timeout=3)
    payload = response.public_payload()

    assert seen["body"]["code"] == "auth-code"
    assert seen["body"]["code_verifier"]
    assert response.id_token == "id.jwt.token"
    assert payload["has_id_token"] is True
    assert payload["has_access_token"] is True
    assert payload["has_refresh_token"] is True
    assert "id.jwt.token" not in repr(payload)
    assert "access-secret" not in repr(payload)
    assert "refresh-secret" not in repr(payload)


@pytest.mark.parametrize(
    ("status", "payload", "message"),
    [
        (500, {}, "HTTP 500"),
        (200, {"error": "invalid_grant"}, "invalid_grant"),
        (200, {"access_token": "missing-id-token"}, "missing id_token"),
        (200, {"id_token": "token", "expires_in": "not-int"}, "integer"),
    ],
)
def test_exchange_oidc_authorization_code_rejects_bad_responses(status, payload, message):
    def transport(_url, _body, _headers, _timeout):
        return status, payload

    with pytest.raises(ValueError, match=message):
        exchange_oidc_authorization_code(_exchange_request(), transport=transport)


def test_fetch_oidc_jwks_validates_keys_list():
    provider = _provider()
    seen = {}

    def transport(url, body, headers, timeout):
        seen.update({"url": url, "body": body, "headers": headers, "timeout": timeout})
        return 200, {"keys": [{"kid": "key-1", "kty": "RSA"}]}

    jwks = fetch_oidc_jwks(provider, transport=transport)

    assert seen["url"] == "https://login.microsoftonline.com/tenant/discovery/v2.0/keys"
    assert jwks["keys"][0]["kid"] == "key-1"


def test_fetch_oidc_jwks_rejects_bad_response():
    provider = _provider()

    def transport(_url, _body, _headers, _timeout):
        return 200, {"no_keys": []}

    with pytest.raises(ValueError, match="keys list"):
        fetch_oidc_jwks(provider, transport=transport)


def test_oidc_jwks_cache_reuses_until_ttl_expires():
    provider = _provider()
    calls = []

    def fetcher(_provider):
        calls.append(len(calls))
        return {"keys": [{"kid": f"key-{len(calls)}", "kty": "RSA"}]}

    cache = OidcJwksCache(ttl_seconds=10)

    first = cache.get(provider, fetcher=fetcher, now=100)
    second = cache.get(provider, fetcher=fetcher, now=105)
    third = cache.get(provider, fetcher=fetcher, now=111)

    assert first is second
    assert third is not first
    assert [key["kid"] for key in (first["keys"][0], third["keys"][0])] == ["key-1", "key-2"]
    assert calls == [0, 1]
