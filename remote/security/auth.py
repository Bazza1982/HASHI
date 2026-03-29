"""
Token authentication for Hashi Remote.
Adapted from Lily Remote — simplified, LAN mode enabled by default.
"""

from typing import Optional
from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from .pairing import PairingManager

_pairing_manager: Optional[PairingManager] = None
_lan_mode: bool = True


def set_pairing_manager(manager: PairingManager) -> None:
    global _pairing_manager
    _pairing_manager = manager


def set_lan_mode(enabled: bool) -> None:
    global _lan_mode
    _lan_mode = enabled


def is_lan_mode() -> bool:
    return _lan_mode


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
    if _pairing_manager:
        client_id = _pairing_manager.verify_token(token)
        if client_id:
            return client_id
    raise HTTPException(status_code=401, detail="Invalid or expired token")
