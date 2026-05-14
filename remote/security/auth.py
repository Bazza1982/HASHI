"""Token authentication helpers for Hashi Remote."""

from typing import Optional
from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from .pairing import PairingManager
from .shared_token import HEADER_AUTH_SCHEME, NonceStore, verify_auth_headers
from ..local_http import is_local_http_host

_pairing_manager: Optional[PairingManager] = None
_lan_mode: bool = True
_shared_token: Optional[str] = None
_nonce_store = NonceStore()


def set_pairing_manager(manager: PairingManager) -> None:
    global _pairing_manager
    _pairing_manager = manager


def set_lan_mode(enabled: bool) -> None:
    global _lan_mode
    _lan_mode = enabled


def is_lan_mode() -> bool:
    return _lan_mode


def set_shared_token(token: str | None) -> None:
    global _shared_token, _nonce_store
    value = str(token or "").strip()
    _shared_token = value or None
    _nonce_store = NonceStore()


def has_shared_token() -> bool:
    return bool(_shared_token)


def protocol_auth_mode() -> str:
    return "shared-token" if _shared_token else "discovery-only"


def _extract_bearer_token(request: Request) -> Optional[str]:
    auth_header = str(request.headers.get("Authorization") or "").strip()
    if not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header[7:].strip()
    return token or None


def _verify_bearer_token_strict(token: str | None) -> Optional[str]:
    if not token or _pairing_manager is None:
        return None
    return _pairing_manager.verify_token(token)


def is_loopback_request(request: Request) -> bool:
    if request.client is None:
        return False
    host = str(request.client.host or "").strip().lower()
    return is_local_http_host(host)


def _has_protocol_auth_headers(request: Request) -> bool:
    return bool(str(request.headers.get(HEADER_AUTH_SCHEME) or "").strip())


def authenticate_request_detailed(
    request: Request,
    *,
    body_bytes: bytes = b"",
    from_instance: str | None = None,
    allow_loopback: bool = False,
    allow_lan: bool = False,
) -> tuple[Optional[str], str]:
    if allow_loopback and is_loopback_request(request):
        return "loopback", "ok"

    if _has_protocol_auth_headers(request):
        ok, reason, authenticated_instance = verify_protocol_request(
            request,
            body_bytes=body_bytes,
            from_instance=from_instance,
        )
        if ok:
            return authenticated_instance or "shared-token", "ok"
        return None, reason

    bearer = _verify_bearer_token_strict(_extract_bearer_token(request))
    if bearer:
        return bearer, "ok"

    if allow_lan and _lan_mode:
        return "lan-client", "ok"

    return None, "auth_required"


def try_authenticate_request(
    request: Request,
    *,
    body_bytes: bytes = b"",
    from_instance: str | None = None,
    allow_loopback: bool = False,
    allow_lan: bool = False,
) -> Optional[str]:
    authenticated, _reason = authenticate_request_detailed(
        request,
        body_bytes=body_bytes,
        from_instance=from_instance,
        allow_loopback=allow_loopback,
        allow_lan=allow_lan,
    )
    return authenticated


def verify_protocol_request(
    request: Request,
    *,
    body_bytes: bytes,
    from_instance: str | None = None,
) -> tuple[bool, str, str | None]:
    return verify_auth_headers(
        headers=request.headers,
        shared_token=_shared_token,
        method=request.method,
        path=request.url.path,
        body_bytes=body_bytes,
        nonce_store=_nonce_store,
        expected_from_instance=from_instance,
    )


class _TokenBearer(HTTPBearer):
    def __init__(self):
        super().__init__(auto_error=False)

    async def __call__(self, request: Request) -> Optional[str]:
        if _lan_mode:
            return "lan-client"
        creds: HTTPAuthorizationCredentials = await super().__call__(request)
        return creds.credentials if creds else None


_token_bearer = _TokenBearer()


async def verify_token(token: str = Depends(_token_bearer)) -> str:
    if _lan_mode:
        return token or "lan-client"
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    client_id = _verify_bearer_token_strict(token)
    if client_id:
        return client_id
    raise HTTPException(status_code=401, detail="Invalid or expired token")
