from __future__ import annotations

from orchestrator.enterprise.auth_providers import load_auth_providers


def test_load_auth_providers_includes_local_provider_by_default():
    providers = load_auth_providers([])

    assert [provider.id for provider in providers] == ["local"]
    assert providers[0].ready is True
    assert providers[0].public_payload()["type"] == "local"


def test_oidc_provider_requires_core_metadata_when_enabled():
    providers = load_auth_providers(
        [
            {
                "type": "oidc",
                "id": "okta",
                "display_name": "Okta",
                "enabled": True,
                "issuer": "https://example.okta.com/oauth2/default",
                "client_id": "hashi",
                "authorization_endpoint": "https://example.okta.com/oauth2/v1/authorize",
            }
        ]
    )

    oidc = providers[1]
    assert oidc.ready is False
    assert "token_endpoint is required for enabled OIDC provider" in oidc.errors
    assert "jwks_uri is required for enabled OIDC provider" in oidc.errors


def test_oidc_public_payload_redacts_secret_material():
    providers = load_auth_providers(
        [
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
                "scopes": ["openid", "email"],
            }
        ]
    )

    payload = providers[1].public_payload()
    assert payload["ready"] is True
    assert payload["scopes"] == ["openid", "email"]
    assert "client_secret" not in payload
    assert "do-not-return" not in repr(payload)


def test_saml_provider_requires_metadata_and_redacts_xml():
    providers = load_auth_providers(
        [
            {
                "type": "saml",
                "id": "okta-saml",
                "display_name": "Okta SAML",
                "enabled": True,
                "metadata_xml": "<EntityDescriptor>secret metadata</EntityDescriptor>",
                "sp_entity_id": "hashi-enterprise",
                "acs_url": "https://hashi.example.com/api/auth/saml/okta-saml/callback",
            }
        ]
    )

    payload = providers[1].public_payload()
    assert providers[1].ready is True
    assert payload["type"] == "saml"
    assert payload["sp_entity_id"] == "hashi-enterprise"
    assert payload["acs_url"] == "https://hashi.example.com/api/auth/saml/okta-saml/callback"
    assert "metadata_xml" not in payload
    assert "secret metadata" not in repr(payload)
