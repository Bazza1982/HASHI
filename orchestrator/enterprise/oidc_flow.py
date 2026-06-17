from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from orchestrator.enterprise.auth_providers import AuthProvider, AuthProviderType


@dataclass(frozen=True)
class OidcAuthorizationStart:
    provider_id: str
    authorization_url: str
    state: str
    nonce: str
    code_verifier: str
    code_challenge: str
    redirect_uri: str
    expires_at: str

    def public_payload(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "authorization_url": self.authorization_url,
            "state": self.state,
            "expires_at": self.expires_at,
        }


def build_oidc_authorization_start(
    provider: AuthProvider,
    *,
    redirect_uri: str,
    ttl_seconds: int = 600,
) -> OidcAuthorizationStart:
    if provider.type != AuthProviderType.OIDC:
        raise ValueError("provider is not OIDC")
    if not provider.ready:
        raise ValueError("OIDC provider is not ready")
    redirect_uri = str(redirect_uri or "").strip()
    if not redirect_uri:
        raise ValueError("redirect_uri is required")
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = _pkce_challenge(code_verifier)
    scopes = " ".join(provider.config.get("scopes") or ["openid", "email", "profile"])
    expires_at = (datetime.now(tz=timezone.utc) + timedelta(seconds=max(60, ttl_seconds))).isoformat()
    query = urlencode(
        {
            "response_type": "code",
            "client_id": provider.config["client_id"],
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    authorization_url = f"{provider.config['authorization_endpoint']}?{query}"
    return OidcAuthorizationStart(
        provider_id=provider.id,
        authorization_url=authorization_url,
        state=state,
        nonce=nonce,
        code_verifier=code_verifier,
        code_challenge=code_challenge,
        redirect_uri=redirect_uri,
        expires_at=expires_at,
    )


def _pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
