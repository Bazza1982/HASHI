from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path


HUMAN_RESTART_SECRET_ENV = "HASHI_HUMAN_RESTART_SECRET"
HUMAN_RESTART_SECRET_FILE = ("secrets", "human_restart_secret.txt")
HUMAN_RESTART_SECRET_KEY = "hashi_human_restart_secret"
HUMAN_RESTART_WINDOW_SECONDS = 300


def human_restart_secret_path(bridge_home: Path) -> Path:
    return bridge_home.joinpath(*HUMAN_RESTART_SECRET_FILE)


def load_human_restart_secret(bridge_home: Path, code_root: Path | None = None) -> str | None:
    env_value = str(os.getenv(HUMAN_RESTART_SECRET_ENV) or "").strip()
    if env_value:
        return env_value

    secret_path = human_restart_secret_path(bridge_home)
    if secret_path.exists():
        try:
            value = secret_path.read_text(encoding="utf-8").strip()
        except Exception:
            value = ""
        if value:
            return value

    for root in (bridge_home, code_root):
        if root is None:
            continue
        secrets_path = root / "secrets.json"
        if not secrets_path.exists():
            continue
        try:
            data = json.loads(secrets_path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        token = str((data or {}).get(HUMAN_RESTART_SECRET_KEY) or "").strip()
        if token:
            return token
    return None


def ensure_human_restart_secret(bridge_home: Path, code_root: Path | None = None) -> str:
    existing = load_human_restart_secret(bridge_home, code_root)
    if existing:
        return existing
    token = secrets.token_hex(32)
    secret_path = human_restart_secret_path(bridge_home)
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text(token, encoding="utf-8")
    return token


def _proof_input(
    *,
    requester: str,
    reason: str,
    human_source: str,
    notify_agent: str,
    timestamp: int,
    nonce: str,
) -> bytes:
    parts = [
        str(requester or "").upper(),
        str(reason or ""),
        str(human_source or "").lower(),
        str(notify_agent or "").lower(),
        str(int(timestamp)),
        str(nonce or ""),
    ]
    return "\n".join(parts).encode("utf-8")


def build_human_restart_proof(
    secret: str,
    *,
    requester: str,
    reason: str,
    human_source: str,
    notify_agent: str,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str | int]:
    ts = int(time.time() if timestamp is None else timestamp)
    token_nonce = str(nonce or secrets.token_hex(16))
    digest = hmac.new(
        secret.encode("utf-8"),
        _proof_input(
            requester=requester,
            reason=reason,
            human_source=human_source,
            notify_agent=notify_agent,
            timestamp=ts,
            nonce=token_nonce,
        ),
        hashlib.sha256,
    ).hexdigest()
    return {"timestamp": ts, "nonce": token_nonce, "digest": digest}


def verify_human_restart_proof(
    secret: str | None,
    *,
    requester: str,
    reason: str,
    human_source: str,
    notify_agent: str,
    proof: dict | None,
    now: float | None = None,
    window_seconds: int = HUMAN_RESTART_WINDOW_SECONDS,
) -> tuple[bool, str]:
    if not secret:
        return False, "human restart secret is not configured"
    if not isinstance(proof, dict):
        return False, "human restart proof is required"
    try:
        timestamp = int(proof.get("timestamp"))
    except Exception:
        return False, "human restart proof timestamp is invalid"
    nonce = str(proof.get("nonce") or "").strip()
    digest = str(proof.get("digest") or "").strip().lower()
    if not nonce or not digest:
        return False, "human restart proof is incomplete"
    current_time = float(time.time() if now is None else now)
    if abs(current_time - timestamp) > int(window_seconds):
        return False, "human restart proof expired"
    expected = build_human_restart_proof(
        secret,
        requester=requester,
        reason=reason,
        human_source=human_source,
        notify_agent=notify_agent,
        timestamp=timestamp,
        nonce=nonce,
    )["digest"]
    if not hmac.compare_digest(str(expected).lower(), digest):
        return False, "human restart proof failed verification"
    return True, "ok"
