from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class AuthProviderType(str, Enum):
    LOCAL = "local"
    OIDC = "oidc"
    SAML = "saml"


@dataclass(frozen=True)
class AuthProvider:
    id: str
    type: AuthProviderType
    display_name: str
    enabled: bool
    config: dict[str, Any]
    errors: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.enabled and not self.errors

    def public_payload(self) -> dict[str, Any]:
        payload = {
            "id": self.id,
            "type": self.type.value,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "ready": self.ready,
            "errors": list(self.errors),
        }
        if self.type == AuthProviderType.OIDC:
            payload["issuer"] = self.config.get("issuer")
            payload["client_id"] = self.config.get("client_id")
            payload["scopes"] = list(self.config.get("scopes") or ["openid", "email", "profile"])
            payload["authorization_endpoint"] = self.config.get("authorization_endpoint")
        if self.type == AuthProviderType.SAML:
            payload["idp_entity_id"] = self.config.get("idp_entity_id")
            payload["sp_entity_id"] = self.config.get("sp_entity_id")
            payload["acs_url"] = self.config.get("acs_url")
            payload["sso_binding"] = self.config.get("sso_binding")
        return payload


def load_auth_providers(records: list[dict] | tuple[dict, ...] | None) -> list[AuthProvider]:
    providers = [_local_provider()]
    for record in records or []:
        if not isinstance(record, dict):
            providers.append(
                AuthProvider(
                    id="invalid",
                    type=AuthProviderType.OIDC,
                    display_name="Invalid provider",
                    enabled=False,
                    config={},
                    errors=("provider config must be an object",),
                )
            )
            continue
        provider_type = str(record.get("type") or "").strip().lower()
        if provider_type == AuthProviderType.OIDC.value:
            providers.append(_oidc_provider(record))
        elif provider_type == AuthProviderType.SAML.value:
            providers.append(_saml_provider(record))
        elif provider_type and provider_type != AuthProviderType.LOCAL.value:
            providers.append(
                AuthProvider(
                    id=_text(record.get("id")) or provider_type,
                    type=AuthProviderType.OIDC,
                    display_name=_text(record.get("display_name")) or provider_type,
                    enabled=bool(record.get("enabled", False)),
                    config={},
                    errors=(f"unsupported auth provider type: {provider_type}",),
                )
            )
    return providers


def _local_provider() -> AuthProvider:
    return AuthProvider(
        id="local",
        type=AuthProviderType.LOCAL,
        display_name="Local password",
        enabled=True,
        config={},
    )


def _oidc_provider(record: dict[str, Any]) -> AuthProvider:
    provider_id = _text(record.get("id")) or "oidc"
    enabled = bool(record.get("enabled", False))
    config = {
        "issuer": _text(record.get("issuer")),
        "client_id": _text(record.get("client_id")),
        "client_secret": _text(record.get("client_secret")),
        "authorization_endpoint": _text(record.get("authorization_endpoint")),
        "token_endpoint": _text(record.get("token_endpoint")),
        "jwks_uri": _text(record.get("jwks_uri")),
        "scopes": _scopes(record.get("scopes")),
    }
    errors = []
    if enabled:
        for key in ("issuer", "client_id", "authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not config.get(key):
                errors.append(f"{key} is required for enabled OIDC provider")
    return AuthProvider(
        id=provider_id,
        type=AuthProviderType.OIDC,
        display_name=_text(record.get("display_name")) or provider_id,
        enabled=enabled,
        config=config,
        errors=tuple(errors),
    )


def _saml_provider(record: dict[str, Any]) -> AuthProvider:
    provider_id = _text(record.get("id")) or "saml"
    enabled = bool(record.get("enabled", False))
    config = {
        "metadata_xml": _text(record.get("metadata_xml")),
        "idp_entity_id": _text(record.get("idp_entity_id")) or _text(record.get("issuer")),
        "sp_entity_id": _text(record.get("sp_entity_id")) or _text(record.get("audience")),
        "acs_url": _text(record.get("acs_url")),
        "default_project_id": _text(record.get("default_project_id")),
        "sso_binding": _text(record.get("sso_binding")),
        "xmlsec1_path": _text(record.get("xmlsec1_path")),
        "xmlsec1_timeout_seconds": _text(record.get("xmlsec1_timeout_seconds")),
    }
    errors = []
    if enabled:
        for key in ("metadata_xml", "sp_entity_id", "acs_url"):
            if not config.get(key):
                errors.append(f"{key} is required for enabled SAML provider")
    return AuthProvider(
        id=provider_id,
        type=AuthProviderType.SAML,
        display_name=_text(record.get("display_name")) or provider_id,
        enabled=enabled,
        config=config,
        errors=tuple(errors),
    )


def _scopes(value) -> list[str]:
    if value is None:
        return ["openid", "email", "profile"]
    if isinstance(value, str):
        raw_items = value.split()
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = []
    scopes = [str(item).strip() for item in raw_items if str(item).strip()]
    return scopes or ["openid", "email", "profile"]


def _text(value) -> str:
    return str(value or "").strip()
