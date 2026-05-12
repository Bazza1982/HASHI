"""Security module — TLS, auth, and pairing for Hashi Remote."""

from .auth import (
    has_shared_token,
    protocol_auth_mode,
    set_lan_mode,
    set_pairing_manager,
    set_shared_token,
    try_authenticate_request,
    verify_protocol_request,
    verify_token,
)
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
from .tls import load_or_generate_cert
from .pairing import PairingManager

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
