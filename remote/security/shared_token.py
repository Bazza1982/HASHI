from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qsl, urlencode


AUTH_SCHEME = "hashi-shared-hmac-v1"
HEADER_AUTH_SCHEME = "X-Hashi-Auth-Scheme"
HEADER_TIMESTAMP = "X-Hashi-Timestamp"
HEADER_NONCE = "X-Hashi-Nonce"
HEADER_DIGEST = "X-Hashi-Digest"
HEADER_FROM_INSTANCE = "X-Hashi-From-Instance"
TIMESTAMP_WINDOW_SECONDS = 300
NONCE_TTL_SECONDS = TIMESTAMP_WINDOW_SECONDS * 2


def load_shared_token(hashi_root: Path | str | None) -> str | None:
    env_value = str(os.getenv("HASHI_REMOTE_SHARED_TOKEN") or "").strip()
    if env_value:
        return env_value

    if hashi_root is None:
        return None

    secrets_path = Path(hashi_root) / "secrets.json"
    if not secrets_path.exists():
        return None

    try:
        data = json.loads(secrets_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None

    token = str((data or {}).get("hashi_remote_shared_token") or "").strip()
    return token or None


def canonical_payload_hash(body_bytes: bytes) -> str:
    return hashlib.sha256(body_bytes).hexdigest()


def canonical_request_target(path: str, query: str | None = None) -> str:
    clean_path = str(path or "")
    clean_query = str(query or "")
    if not clean_query:
        return clean_path
    canonical_items = parse_qsl(clean_query, keep_blank_values=True)
    canonical_items.sort()
    canonical_query = urlencode(canonical_items, doseq=True)
    return f"{clean_path}?{canonical_query}" if canonical_query else clean_path


def build_hmac_input(
    *,
    method: str,
    path: str,
    from_instance: str,
    timestamp: int,
    nonce: str,
    body_bytes: bytes,
) -> bytes:
    parts = [
        str(method or "").upper(),
        canonical_request_target(path),
        str(from_instance or "").upper(),
        str(int(timestamp)),
        str(nonce or ""),
        canonical_payload_hash(body_bytes),
    ]
    return "\n".join(parts).encode("utf-8")


def build_auth_headers(
    *,
    shared_token: str,
    method: str,
    path: str,
    from_instance: str,
    body_bytes: bytes,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    ts = int(time.time() if timestamp is None else timestamp)
    token_nonce = nonce or secrets.token_hex(16)
    digest = hmac.new(
        shared_token.encode("utf-8"),
        build_hmac_input(
            method=method,
            path=path,
            from_instance=from_instance,
            timestamp=ts,
            nonce=token_nonce,
            body_bytes=body_bytes,
        ),
        hashlib.sha256,
    ).hexdigest()
    return {
        HEADER_AUTH_SCHEME: AUTH_SCHEME,
        HEADER_TIMESTAMP: str(ts),
        HEADER_NONCE: token_nonce,
        HEADER_DIGEST: digest,
        HEADER_FROM_INSTANCE: str(from_instance or "").upper(),
    }


class NonceStore:
    def __init__(self, ttl_seconds: int = NONCE_TTL_SECONDS):
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._entries: dict[tuple[str, str], float] = {}

    def _purge(self, now: float) -> None:
        expired = [key for key, deadline in self._entries.items() if deadline <= now]
        for key in expired:
            self._entries.pop(key, None)

    def remember(self, *, from_instance: str, nonce: str, now: float | None = None) -> bool:
        ts = float(time.time() if now is None else now)
        self._purge(ts)
        key = (str(from_instance or "").upper(), str(nonce or ""))
        if not key[0] or not key[1]:
            return False
        if key in self._entries:
            return False
        self._entries[key] = ts + self._ttl_seconds
        return True


def verify_auth_headers(
    *,
    headers: Mapping[str, str],
    shared_token: str | None,
    method: str,
    path: str,
    body_bytes: bytes,
    nonce_store: NonceStore,
    expected_from_instance: str | None = None,
    now: float | None = None,
    timestamp_window_seconds: int = TIMESTAMP_WINDOW_SECONDS,
) -> tuple[bool, str, str | None]:
    if not shared_token:
        return False, "auth_required", None

    auth_scheme = str(headers.get(HEADER_AUTH_SCHEME) or "").strip()
    timestamp_raw = str(headers.get(HEADER_TIMESTAMP) or "").strip()
    nonce = str(headers.get(HEADER_NONCE) or "").strip()
    digest = str(headers.get(HEADER_DIGEST) or "").strip().lower()
    from_instance = str(headers.get(HEADER_FROM_INSTANCE) or "").strip().upper()

    if not all([auth_scheme, timestamp_raw, nonce, digest, from_instance]):
        return False, "auth_required", None
    if auth_scheme != AUTH_SCHEME:
        return False, "auth_failed", None

    if expected_from_instance and from_instance != str(expected_from_instance).strip().upper():
        return False, "auth_failed", None

    try:
        timestamp = int(timestamp_raw)
    except Exception:
        return False, "auth_failed", None

    current_time = float(time.time() if now is None else now)
    if abs(current_time - timestamp) > int(timestamp_window_seconds):
        return False, "auth_failed", from_instance

    expected_digest = build_auth_headers(
        shared_token=shared_token,
        method=method,
        path=path,
        from_instance=from_instance,
        body_bytes=body_bytes,
        timestamp=timestamp,
        nonce=nonce,
    )[HEADER_DIGEST].lower()
    if not hmac.compare_digest(expected_digest, digest):
        return False, "auth_failed", from_instance

    if not nonce_store.remember(from_instance=from_instance, nonce=nonce, now=current_time):
        return False, "auth_failed", from_instance

    return True, "ok", from_instance
