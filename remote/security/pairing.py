"""
Pairing logic for Hashi Remote.
Adapted from Lily Remote — storage path changed to ~/.hashi-remote/
"""

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class PairingState(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class PairingRequest:
    client_id: str
    client_name: str
    challenge: str
    created_at: float
    expires_at: float
    state: PairingState = PairingState.PENDING


@dataclass
class PairedClient:
    client_id: str
    client_name: str
    token_hash: str
    paired_at: float
    instance_id: str = "unknown"  # Which HASHI instance this client represents


class PairingManager:
    """Manages pairing requests and paired clients for Hashi Remote."""

    CHALLENGE_LENGTH = 32
    CHALLENGE_EXPIRY_SECONDS = 300
    TOKEN_LENGTH = 32

    def __init__(self, storage_dir: Optional[Path] = None, lan_mode: bool = True):
        self._storage_dir = storage_dir or (Path.home() / ".hashi-remote")
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._paired_file = self._storage_dir / "paired_instances.json"
        self._pending: dict[str, PairingRequest] = {}
        self._paired: dict[str, PairedClient] = {}
        self._lan_mode = lan_mode
        self._approval_callbacks: list = []
        self._load_paired()

    @property
    def lan_mode(self) -> bool:
        return self._lan_mode

    def set_lan_mode(self, enabled: bool) -> None:
        self._lan_mode = enabled

    def _load_paired(self) -> None:
        if not self._paired_file.exists():
            return
        try:
            data = json.loads(self._paired_file.read_text(encoding="utf-8"))
            for entry in data.get("clients", []):
                client = PairedClient(**entry)
                self._paired[client.client_id] = client
        except Exception:
            pass

    def _save_paired(self) -> None:
        data = {"clients": [
            {"client_id": c.client_id, "client_name": c.client_name,
             "token_hash": c.token_hash, "paired_at": c.paired_at,
             "instance_id": c.instance_id}
            for c in self._paired.values()
        ]}
        self._paired_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            self._paired_file.chmod(0o600)
        except OSError:
            pass

    def create_pairing_request(self, client_id: str, client_name: str) -> PairingRequest:
        now = time.time()
        req = PairingRequest(
            client_id=client_id,
            client_name=client_name,
            challenge=secrets.token_hex(self.CHALLENGE_LENGTH),
            created_at=now,
            expires_at=now + self.CHALLENGE_EXPIRY_SECONDS,
        )
        self._pending[client_id] = req
        return req

    def approve_request(self, client_id: str) -> Optional[str]:
        req = self._pending.get(client_id)
        if not req or time.time() > req.expires_at:
            return None
        token = secrets.token_hex(self.TOKEN_LENGTH)
        client = PairedClient(
            client_id=client_id,
            client_name=req.client_name,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            paired_at=time.time(),
        )
        self._paired[client_id] = client
        del self._pending[client_id]
        self._save_paired()
        return token

    def reject_request(self, client_id: str) -> bool:
        if client_id in self._pending:
            del self._pending[client_id]
            return True
        return False

    def approve_request_direct(self, client_id: str, client_name: str) -> str:
        """Create and immediately approve a pairing request (for LAN auto-approve)."""
        now = time.time()
        self._pending[client_id] = PairingRequest(
            client_id=client_id,
            client_name=client_name,
            challenge=secrets.token_hex(self.CHALLENGE_LENGTH),
            created_at=now,
            expires_at=now + self.CHALLENGE_EXPIRY_SECONDS,
        )
        token = self.approve_request(client_id)
        return token

    def verify_token(self, token: str) -> Optional[str]:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        for client in self._paired.values():
            if hmac.compare_digest(client.token_hash, token_hash):
                return client.client_id
        return None

    def get_pending_requests(self) -> list[PairingRequest]:
        now = time.time()
        return [r for r in self._pending.values() if r.expires_at > now]

    def get_paired_clients(self) -> list[PairedClient]:
        return list(self._paired.values())

    def get_request(self, client_id: str) -> Optional[PairingRequest]:
        return self._pending.get(client_id)

    def is_auto_approved(self) -> bool:
        """In LAN mode, all pairing requests are auto-approved."""
        return self._lan_mode
