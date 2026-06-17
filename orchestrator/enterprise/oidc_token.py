from __future__ import annotations

import base64
import hashlib
import hmac
import json
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


def verify_oidc_id_token(
    provider: AuthProvider,
    flow: OidcAuthorizationStart,
    id_token: str,
    jwks: dict[str, Any],
    *,
    now: datetime | None = None,
    clock_skew_seconds: int = 60,
) -> OidcValidatedClaims:
    header, claims, signed_part, signature = _decode_compact_jwt(id_token)
    algorithm = str(header.get("alg") or "").strip()
    if algorithm != "RS256":
        raise ValueError("unsupported OIDC ID token algorithm")
    kid = str(header.get("kid") or "").strip()
    if not kid:
        raise ValueError("OIDC ID token kid is required")
    key = _select_jwks_key(jwks, kid=kid, algorithm=algorithm)
    if not _verify_rs256_signature(key, signed_part, signature):
        raise ValueError("OIDC ID token signature verification failed")
    return validate_oidc_id_token_claims(
        provider,
        flow,
        claims,
        now=now,
        clock_skew_seconds=clock_skew_seconds,
    )


def _decode_compact_jwt(id_token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    parts = str(id_token or "").strip().split(".")
    if len(parts) != 3 or not parts[0] or not parts[1]:
        raise ValueError("OIDC ID token must be a compact JWT")
    header = _decode_json_part(parts[0], "header")
    claims = _decode_json_part(parts[1], "payload")
    signature = _b64url_decode(parts[2])
    return header, claims, f"{parts[0]}.{parts[1]}".encode("ascii"), signature


def _decode_json_part(value: str, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(_b64url_decode(value).decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"OIDC ID token {label} is invalid") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"OIDC ID token {label} must be an object")
    return decoded


def _select_jwks_key(jwks: dict[str, Any], *, kid: str, algorithm: str) -> dict[str, Any]:
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    if not isinstance(keys, list):
        raise ValueError("JWKS keys must be a list")
    for key in keys:
        if not isinstance(key, dict) or str(key.get("kid") or "") != kid:
            continue
        if str(key.get("kty") or "") != "RSA":
            raise ValueError("OIDC JWKS key must be RSA")
        if key.get("use") not in (None, "sig"):
            raise ValueError("OIDC JWKS key is not a signing key")
        key_alg = str(key.get("alg") or algorithm)
        if key_alg != algorithm:
            raise ValueError("OIDC JWKS key algorithm mismatch")
        return key
    raise ValueError("OIDC JWKS key not found")


def _verify_rs256_signature(key: dict[str, Any], signed_part: bytes, signature: bytes) -> bool:
    try:
        exponent = _b64url_uint(str(key["e"]))
        modulus = _b64url_uint(str(key["n"]))
    except Exception as exc:
        raise ValueError("OIDC JWKS RSA key is invalid") from exc
    modulus_len = (modulus.bit_length() + 7) // 8
    if len(signature) != modulus_len:
        return False
    verified = pow(int.from_bytes(signature, "big"), exponent, modulus).to_bytes(modulus_len, "big")
    expected = _pkcs1_v1_5_sha256_encoded(signed_part, modulus_len)
    return hmac.compare_digest(verified, expected)


def _pkcs1_v1_5_sha256_encoded(message: bytes, length: int) -> bytes:
    digest_info_prefix = bytes.fromhex("3031300d060960864801650304020105000420")
    digest_info = digest_info_prefix + hashlib.sha256(message).digest()
    padding_len = length - len(digest_info) - 3
    if padding_len < 8:
        raise ValueError("OIDC JWKS RSA key is too small for RS256")
    return b"\x00\x01" + (b"\xff" * padding_len) + b"\x00" + digest_info


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


def _b64url_decode(value: str) -> bytes:
    value = str(value or "")
    padding_len = (-len(value)) % 4
    return base64.urlsafe_b64decode((value + ("=" * padding_len)).encode("ascii"))


def _b64url_uint(value: str) -> int:
    return int.from_bytes(_b64url_decode(value), "big")
