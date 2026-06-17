from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from orchestrator.enterprise.auth_providers import AuthProvider, AuthProviderType
from orchestrator.enterprise.oidc_flow import OidcAuthorizationStart


@dataclass(frozen=True)
class OidcValidatedClaims:
    provider_id: str
    issuer: str
    subject: str
    audience: tuple[str, ...]
    email: str | None
    expires_at: int
    issued_at: int | None
    nonce: str
    claims: dict[str, Any]

    def public_payload(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "issuer": self.issuer,
            "subject": self.subject,
            "audience": list(self.audience),
            "email": self.email,
            "expires_at": self.expires_at,
            "issued_at": self.issued_at,
        }


def validate_oidc_id_token_claims(
    provider: AuthProvider,
    flow: OidcAuthorizationStart,
    claims: dict[str, Any],
    *,
    now: datetime | None = None,
    clock_skew_seconds: int = 60,
) -> OidcValidatedClaims:
    """Validate OIDC ID-token claims after cryptographic JWT verification.

    This function deliberately does not verify the JWT signature. The caller must
    pass claims only after validating the token against the provider JWKS.
    """
    if provider.type != AuthProviderType.OIDC:
        raise ValueError("provider is not OIDC")
    if not provider.ready:
        raise ValueError("OIDC provider is not ready")
    if provider.id != flow.provider_id:
        raise ValueError("OIDC flow provider mismatch")
    if not isinstance(claims, dict):
        raise ValueError("claims must be an object")

    now_ts = int((now or datetime.now(tz=timezone.utc)).timestamp())
    skew = max(0, int(clock_skew_seconds))
    issuer = _required_text_claim(claims, "iss")
    if issuer != provider.config.get("issuer"):
        raise ValueError("issuer mismatch")

    subject = _required_text_claim(claims, "sub")
    audience = _audiences(claims.get("aud"))
    if provider.config.get("client_id") not in audience:
        raise ValueError("audience mismatch")

    expires_at = _required_int_claim(claims, "exp")
    if expires_at <= now_ts - skew:
        raise ValueError("ID token is expired")

    not_before = _optional_int_claim(claims, "nbf")
    if not_before is not None and not_before > now_ts + skew:
        raise ValueError("ID token is not yet valid")

    issued_at = _optional_int_claim(claims, "iat")
    if issued_at is not None and issued_at > now_ts + skew:
        raise ValueError("ID token issued_at is in the future")

    nonce = _required_text_claim(claims, "nonce")
    if nonce != flow.nonce:
        raise ValueError("nonce mismatch")

    email = str(claims.get("email") or "").strip().lower() or None
    return OidcValidatedClaims(
        provider_id=provider.id,
        issuer=issuer,
        subject=subject,
        audience=tuple(audience),
        email=email,
        expires_at=expires_at,
        issued_at=issued_at,
        nonce=nonce,
        claims=dict(claims),
    )


def _required_text_claim(claims: dict[str, Any], key: str) -> str:
    value = str(claims.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} claim is required")
    return value


def _required_int_claim(claims: dict[str, Any], key: str) -> int:
    value = _optional_int_claim(claims, key)
    if value is None:
        raise ValueError(f"{key} claim is required")
    return value


def _optional_int_claim(claims: dict[str, Any], key: str) -> int | None:
    raw = claims.get(key)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} claim must be an integer timestamp") from exc


def _audiences(value: Any) -> list[str]:
    if isinstance(value, str):
        audiences = [value]
    elif isinstance(value, (list, tuple, set)):
        audiences = [str(item).strip() for item in value]
    else:
        audiences = []
    audiences = [item for item in audiences if item]
    if not audiences:
        raise ValueError("aud claim is required")
    return audiences
