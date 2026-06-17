from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib import request as urllib_request
from urllib.parse import urlencode

from orchestrator.enterprise.auth_providers import AuthProvider, AuthProviderType
from orchestrator.enterprise.oidc_exchange import OidcTokenExchangeRequest


JsonTransport = Callable[[str, dict[str, str] | None, dict[str, str], float], tuple[int, dict[str, Any]]]


@dataclass(frozen=True)
class OidcTokenResponse:
    provider_id: str
    id_token: str
    access_token: str | None
    refresh_token: str | None
    token_type: str | None
    expires_in: int | None
    scope: str | None
    raw: dict[str, Any]

    def public_payload(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "has_id_token": bool(self.id_token),
            "has_access_token": bool(self.access_token),
            "has_refresh_token": bool(self.refresh_token),
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "scope": self.scope,
        }


class OidcJwksCache:
    def __init__(self, *, ttl_seconds: int = 300):
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._items: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(
        self,
        provider: AuthProvider,
        *,
        fetcher: Callable[[AuthProvider], dict[str, Any]] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        now_ts = time.time() if now is None else float(now)
        cached = self._items.get(provider.id)
        if cached and cached[0] > now_ts:
            return cached[1]
        jwks = (fetcher or fetch_oidc_jwks)(provider)
        expiry = now_ts + self.ttl_seconds
        self._items[provider.id] = (expiry, jwks)
        return jwks

    def clear(self, provider_id: str | None = None) -> None:
        if provider_id is None:
            self._items.clear()
        else:
            self._items.pop(provider_id, None)


def exchange_oidc_authorization_code(
    exchange_request: OidcTokenExchangeRequest,
    *,
    transport: JsonTransport | None = None,
    timeout: float = 10.0,
) -> OidcTokenResponse:
    status, payload = (transport or _default_post_form_json)(
        exchange_request.token_endpoint,
        exchange_request.body,
        exchange_request.headers,
        timeout,
    )
    if status < 200 or status >= 300:
        raise ValueError(f"OIDC token endpoint returned HTTP {status}")
    if not isinstance(payload, dict):
        raise ValueError("OIDC token response must be a JSON object")
    provider_error = str(payload.get("error") or "").strip()
    if provider_error:
        raise ValueError(f"OIDC token endpoint error: {provider_error}")
    id_token = str(payload.get("id_token") or "").strip()
    if not id_token:
        raise ValueError("OIDC token response missing id_token")
    return OidcTokenResponse(
        provider_id=exchange_request.provider_id,
        id_token=id_token,
        access_token=_optional_text(payload.get("access_token")),
        refresh_token=_optional_text(payload.get("refresh_token")),
        token_type=_optional_text(payload.get("token_type")),
        expires_in=_optional_int(payload.get("expires_in")),
        scope=_optional_text(payload.get("scope")),
        raw=dict(payload),
    )


def fetch_oidc_jwks(
    provider: AuthProvider,
    *,
    transport: JsonTransport | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    if provider.type != AuthProviderType.OIDC:
        raise ValueError("provider is not OIDC")
    if not provider.ready:
        raise ValueError("OIDC provider is not ready")
    jwks_uri = str(provider.config.get("jwks_uri") or "").strip()
    if not jwks_uri:
        raise ValueError("jwks_uri is required")
    status, payload = (transport or _default_get_json)(jwks_uri, None, {}, timeout)
    if status < 200 or status >= 300:
        raise ValueError(f"OIDC JWKS endpoint returned HTTP {status}")
    if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
        raise ValueError("OIDC JWKS response must include keys list")
    return payload


def _default_post_form_json(
    url: str,
    body: dict[str, str] | None,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    encoded = urlencode(body or {}).encode("utf-8")
    req = urllib_request.Request(url, data=encoded, headers=headers, method="POST")
    with urllib_request.urlopen(req, timeout=timeout) as response:
        return int(response.status), _json_response(response.read())


def _default_get_json(
    url: str,
    _body: dict[str, str] | None,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    req = urllib_request.Request(url, headers=headers, method="GET")
    with urllib_request.urlopen(req, timeout=timeout) as response:
        return int(response.status), _json_response(response.read())


def _json_response(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise ValueError("OIDC endpoint returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("OIDC endpoint returned non-object JSON")
    return payload


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("OIDC numeric response field must be an integer") from exc
