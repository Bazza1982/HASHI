from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator.enterprise.auth_providers import AuthProvider, AuthProviderType
from orchestrator.enterprise.oidc_flow import OidcAuthorizationStart


@dataclass(frozen=True)
class OidcTokenExchangeRequest:
    provider_id: str
    token_endpoint: str
    method: str
    headers: dict[str, str]
    body: dict[str, str]

    def public_payload(self) -> dict[str, Any]:
        sensitive_fields = {"code", "code_verifier", "client_secret"}
        return {
            "provider_id": self.provider_id,
            "token_endpoint": self.token_endpoint,
            "method": self.method,
            "body_fields": sorted(key for key in self.body if key not in sensitive_fields),
            "redacted_body_field_count": sum(1 for key in self.body if key in sensitive_fields),
            "uses_client_secret": bool(self.body.get("client_secret")),
        }


@dataclass(frozen=True)
class OidcMappedIdentity:
    provider_id: str
    org_id: str
    subject: str
    external_user_id: str
    email: str
    display_name: str

    def public_payload(self) -> dict[str, str]:
        return {
            "provider_id": self.provider_id,
            "org_id": self.org_id,
            "external_user_id": self.external_user_id,
            "email": self.email,
            "display_name": self.display_name,
        }


def build_oidc_token_exchange_request(
    provider: AuthProvider,
    flow: OidcAuthorizationStart,
    *,
    code: str,
) -> OidcTokenExchangeRequest:
    if provider.type != AuthProviderType.OIDC:
        raise ValueError("provider is not OIDC")
    if not provider.ready:
        raise ValueError("OIDC provider is not ready")
    if provider.id != flow.provider_id:
        raise ValueError("OIDC flow provider mismatch")
    code = str(code or "").strip()
    if not code:
        raise ValueError("authorization code is required")
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": flow.redirect_uri,
        "client_id": provider.config["client_id"],
        "code_verifier": flow.code_verifier,
    }
    client_secret = str(provider.config.get("client_secret") or "").strip()
    if client_secret:
        body["client_secret"] = client_secret
    return OidcTokenExchangeRequest(
        provider_id=provider.id,
        token_endpoint=provider.config["token_endpoint"],
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=body,
    )


def map_oidc_claims(
    *,
    provider_id: str,
    org_id: str,
    claims: dict[str, Any],
    subject_claim: str = "sub",
    email_claim: str = "email",
    name_claim: str = "name",
) -> OidcMappedIdentity:
    subject = str(claims.get(subject_claim) or "").strip()
    if not subject:
        raise ValueError(f"{subject_claim} claim is required")
    email = str(claims.get(email_claim) or "").strip().lower()
    if "@" not in email:
        raise ValueError(f"{email_claim} claim must be an email address")
    display_name = (
        str(claims.get(name_claim) or "").strip()
        or str(claims.get("preferred_username") or "").strip()
        or email
    )
    provider_id = str(provider_id or "").strip()
    org_id = str(org_id or "").strip()
    if not provider_id:
        raise ValueError("provider_id is required")
    if not org_id:
        raise ValueError("org_id is required")
    return OidcMappedIdentity(
        provider_id=provider_id,
        org_id=org_id,
        subject=subject,
        external_user_id=f"oidc:{provider_id}:{subject}",
        email=email,
        display_name=display_name,
    )
