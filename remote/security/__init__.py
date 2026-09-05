"""Security module — TLS, auth, and pairing for Hashi Remote.

Remote and TLS dependencies are optional installation profiles. Keep the
stdlib-only shared-token helpers importable by core orchestrator modules, and
load FastAPI/cryptography-backed surfaces only when callers actually request
them.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .shared_token import (
    AUTH_SCHEME,
    HEADER_AUTH_SCHEME,
    HEADER_DIGEST,
    HEADER_FROM_INSTANCE,
    HEADER_NONCE,
    HEADER_TIMESTAMP,
    NONCE_TTL_SECONDS,
    TIMESTAMP_WINDOW_SECONDS,
    build_auth_headers,
    load_shared_token,
)

_LAZY_EXPORTS = {
    "PairingManager": (".pairing", "PairingManager"),
    "has_shared_token": (".auth", "has_shared_token"),
    "load_or_generate_cert": (".tls", "load_or_generate_cert"),
    "protocol_auth_mode": (".auth", "protocol_auth_mode"),
    "set_lan_mode": (".auth", "set_lan_mode"),
    "set_pairing_manager": (".auth", "set_pairing_manager"),
    "set_shared_token": (".auth", "set_shared_token"),
    "try_authenticate_request": (".auth", "try_authenticate_request"),
    "verify_protocol_request": (".auth", "verify_protocol_request"),
    "verify_token": (".auth", "verify_token"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

__all__ = [
    "AUTH_SCHEME",
    "HEADER_AUTH_SCHEME",
    "HEADER_DIGEST",
    "HEADER_FROM_INSTANCE",
    "HEADER_NONCE",
    "HEADER_TIMESTAMP",
    "NONCE_TTL_SECONDS",
    "PairingManager",
    "TIMESTAMP_WINDOW_SECONDS",
    "build_auth_headers",
    "has_shared_token",
    "load_or_generate_cert",
    "load_shared_token",
    "protocol_auth_mode",
    "set_lan_mode",
    "set_pairing_manager",
    "set_shared_token",
    "try_authenticate_request",
    "verify_protocol_request",
    "verify_token",
]
