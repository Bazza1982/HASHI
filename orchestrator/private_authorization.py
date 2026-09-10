"""Per-message private authorization proofs for HASHI Frontend Connectors.

The shared secret is instance configuration.  It is used only to create or
verify a short-lived HMAC proof and is never returned in a result destined for
PAO, PCM, logs, or a Provider.  Network authentication remains a separate
concern: these proofs add narrowly mapped disclosure scopes to one message and
never decide whether an HChat message may be delivered.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PRIVATE_AUTHORIZATION_TYPE = "hashi.private-authorization-proof"
PRIVATE_AUTHORIZATION_VERSION = 1
PRIVATE_AUTHORIZATION_CONFIG_KEY = "hashi_private_shared_credentials"
DEFAULT_PROOF_TTL_SECONDS = 90
MAX_PROOF_TTL_SECONDS = 300
MAX_PROOFS_PER_MESSAGE = 16
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PRIVATE_AUTHORIZATION_CAPABILITIES = {
    "type": "hashi.private-authorization-capabilities",
    "version": PRIVATE_AUTHORIZATION_VERSION,
    "proof_type": PRIVATE_AUTHORIZATION_TYPE,
    "algorithm": "hmac-sha256",
    "default_ttl_seconds": DEFAULT_PROOF_TTL_SECONDS,
    "max_ttl_seconds": MAX_PROOF_TTL_SECONDS,
    "max_proofs_per_message": MAX_PROOFS_PER_MESSAGE,
    "binding_fields": [
        "message_id",
        "from_instance",
        "from_agent",
        "to_instance",
        "to_agent",
        "content_sha256",
        "resources",
    ],
    "optional_for_hchat": True,
    "raw_secret_on_wire": False,
}


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _clean_identifier(value: Any, *, field: str, max_length: int = 128) -> str:
    clean = str(value or "").strip()
    if (
        not clean
        or len(clean) > max_length
        or any(ord(character) < 33 or ord(character) > 126 for character in clean)
    ):
        raise ValueError(f"invalid {field}")
    return clean


def _clean_instance(value: Any) -> str:
    return _clean_identifier(value, field="instance").upper()


def _clean_agent(value: Any) -> str:
    return _clean_identifier(value, field="agent").casefold()


def _clean_resources(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError("resources must be a list")
    result = sorted(
        {
            _clean_identifier(item, field="resource", max_length=256)
            for item in value
        }
    )
    if len(result) > 64:
        raise ValueError("too many resources")
    return result


def authorization_content_sha256(value: Any) -> str:
    """Return the canonical digest bound to one authorization-bearing message."""

    if isinstance(value, Mapping):
        payload = _canonical_json(value)
    elif isinstance(value, (list, tuple)):
        payload = json.dumps(
            list(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    else:
        payload = str(value or "").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalize_authorization_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("authorization binding must be an object")
    content_sha256 = str(value.get("content_sha256") or "").strip().casefold()
    if not _SHA256_RE.fullmatch(content_sha256):
        raise ValueError("invalid authorization content_sha256")
    return {
        "message_id": _clean_identifier(value.get("message_id"), field="message_id"),
        "from_instance": _clean_instance(value.get("from_instance")),
        "from_agent": _clean_agent(value.get("from_agent")),
        "to_instance": _clean_instance(value.get("to_instance")),
        "to_agent": _clean_agent(value.get("to_agent")),
        "content_sha256": content_sha256,
        "resources": _clean_resources(value.get("resources")),
    }


def _credential_map(value: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, Mapping):
        return {}
    nested = value.get("credentials")
    source = nested if isinstance(nested, Mapping) else value
    return {
        str(key): item
        for key, item in source.items()
        if isinstance(item, Mapping)
    }


def load_private_authorization_config(hashi_root: Path | str | None) -> dict[str, Any]:
    """Load the ignored receiver/sender credential map without logging secrets."""

    if hashi_root is None:
        return {}
    path = Path(hashi_root) / "secrets.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    configured = data.get(PRIVATE_AUTHORIZATION_CONFIG_KEY) if isinstance(data, Mapping) else None
    return dict(configured) if isinstance(configured, Mapping) else {}


def _parse_deadline(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _credential_secret(record: Mapping[str, Any]) -> str:
    secret = str(record.get("secret") or "")
    if len(secret.encode("utf-8")) < 16:
        raise ValueError("private credential secret must be at least 16 bytes")
    return secret


def _proof_claims(
    *,
    credential_id: str,
    binding: Mapping[str, Any],
    issued_at: int,
    expires_at: int,
    nonce: str,
) -> dict[str, Any]:
    return {
        "type": PRIVATE_AUTHORIZATION_TYPE,
        "version": PRIVATE_AUTHORIZATION_VERSION,
        "credential_id": credential_id,
        "issued_at": int(issued_at),
        "expires_at": int(expires_at),
        "nonce": nonce,
        "binding": dict(binding),
    }


def build_private_authorization_proofs(
    config: Mapping[str, Any] | None,
    *,
    credential_ids: Iterable[str],
    binding: Mapping[str, Any],
    now: float | None = None,
    ttl_seconds: int = DEFAULT_PROOF_TTL_SECONDS,
) -> list[dict[str, Any]]:
    """Build proofs only for the credentials explicitly selected this message."""

    normalized_binding = normalize_authorization_binding(binding)
    records = _credential_map(config)
    selected: list[str] = []
    for value in credential_ids:
        credential_id = _clean_identifier(value, field="credential_id")
        if credential_id not in selected:
            selected.append(credential_id)
    if len(selected) > MAX_PROOFS_PER_MESSAGE:
        raise ValueError("too many private credentials selected")
    timestamp = int(time.time() if now is None else now)
    ttl = min(max(1, int(ttl_seconds)), MAX_PROOF_TTL_SECONDS)
    proofs: list[dict[str, Any]] = []
    for credential_id in selected:
        record = records.get(credential_id)
        if record is None:
            raise ValueError(f"private credential is not configured: {credential_id}")
        secret = _credential_secret(record)
        claims = _proof_claims(
            credential_id=credential_id,
            binding=normalized_binding,
            issued_at=timestamp,
            expires_at=timestamp + ttl,
            nonce=secrets.token_hex(16),
        )
        claims["digest"] = hmac.new(
            secret.encode("utf-8"), _canonical_json(claims), hashlib.sha256
        ).hexdigest()
        proofs.append(claims)
    return proofs


class PrivateAuthorizationStore:
    """Persistent nonce ledger owned by PAO for replay-safe verification."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS private_authorization_nonces (
                    credential_id TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    binding_sha256 TEXT NOT NULL,
                    proof_sha256 TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    first_verified_at INTEGER NOT NULL,
                    PRIMARY KEY(credential_id, nonce)
                )
                """
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connection(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path), timeout=10)

    def remember(
        self,
        *,
        credential_id: str,
        nonce: str,
        message_id: str,
        binding_sha256: str,
        proof_sha256: str,
        expires_at: int,
        now: int,
    ) -> str:
        """Return ``new``, ``same`` (idempotent retry), or ``replay``."""

        with self._lock, self._connection() as connection:
            connection.execute(
                "DELETE FROM private_authorization_nonces WHERE expires_at < ?",
                (int(now) - MAX_PROOF_TTL_SECONDS,),
            )
            row = connection.execute(
                """SELECT message_id, binding_sha256, proof_sha256
                   FROM private_authorization_nonces
                   WHERE credential_id=? AND nonce=?""",
                (credential_id, nonce),
            ).fetchone()
            if row is not None:
                return (
                    "same"
                    if tuple(row) == (message_id, binding_sha256, proof_sha256)
                    else "replay"
                )
            connection.execute(
                """
                INSERT INTO private_authorization_nonces(
                    credential_id, nonce, message_id, binding_sha256,
                    proof_sha256, expires_at, first_verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    credential_id,
                    nonce,
                    message_id,
                    binding_sha256,
                    proof_sha256,
                    int(expires_at),
                    int(now),
                ),
            )
        return "new"


def _failure(credential_id: str, state: str, *, proof_id: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {
        "credential_id": credential_id or "unknown",
        "state": state,
        "verification": "runtime_rejected",
    }
    if proof_id:
        result["proof_id"] = proof_id
    return result


def _allowed(value: Any, actual: str, *, fold: bool = False) -> bool:
    if value in (None, ""):
        return True
    if not isinstance(value, (list, tuple, set, frozenset)):
        return False
    if not value:
        return True
    normalizer = str.casefold if fold else str.upper
    return normalizer(actual) in {normalizer(str(item).strip()) for item in value}


def verify_private_authorization_proofs(
    config: Mapping[str, Any] | None,
    *,
    proofs: Iterable[Mapping[str, Any]],
    binding: Mapping[str, Any],
    nonce_store: PrivateAuthorizationStore,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Verify proofs and return only non-secret receiver-owned facts."""

    normalized_binding = normalize_authorization_binding(binding)
    records = _credential_map(config)
    timestamp = int(time.time() if now is None else now)
    supplied = list(proofs or ())
    if len(supplied) > MAX_PROOFS_PER_MESSAGE:
        supplied = supplied[:MAX_PROOFS_PER_MESSAGE]
    results: list[dict[str, Any]] = []
    for raw in supplied:
        if not isinstance(raw, Mapping):
            results.append(_failure("unknown", "invalid"))
            continue
        credential_id = str(raw.get("credential_id") or "").strip()
        proof_id = ""
        try:
            credential_id = _clean_identifier(
                credential_id, field="credential_id"
            )
            claims = dict(raw)
            digest = str(claims.pop("digest", "")).strip().lower()
            proof_id = "proof-" + hashlib.sha256(
                _canonical_json(dict(raw))
            ).hexdigest()[:24]
            if (
                claims.get("type") != PRIVATE_AUTHORIZATION_TYPE
                or claims.get("version") != PRIVATE_AUTHORIZATION_VERSION
                or len(digest) != 64
            ):
                raise ValueError("invalid proof envelope")
            issued_at = int(claims.get("issued_at"))
            expires_at = int(claims.get("expires_at"))
            nonce = _clean_identifier(claims.get("nonce"), field="nonce")
            proof_binding = normalize_authorization_binding(claims.get("binding"))
        except (TypeError, ValueError):
            results.append(_failure(credential_id, "invalid", proof_id=proof_id))
            continue

        record = records.get(credential_id)
        if record is None:
            results.append(_failure(credential_id, "invalid", proof_id=proof_id))
            continue
        if bool(record.get("revoked")):
            results.append(_failure(credential_id, "revoked", proof_id=proof_id))
            continue
        configured_deadline = _parse_deadline(record.get("expires_at"))
        if configured_deadline is not None and timestamp >= configured_deadline:
            results.append(_failure(credential_id, "expired", proof_id=proof_id))
            continue
        if (
            expires_at <= timestamp
            or issued_at > timestamp + 30
            or expires_at <= issued_at
            or expires_at - issued_at > MAX_PROOF_TTL_SECONDS
        ):
            results.append(_failure(credential_id, "expired", proof_id=proof_id))
            continue
        if proof_binding != normalized_binding:
            results.append(_failure(credential_id, "invalid", proof_id=proof_id))
            continue
        if not _allowed(
            record.get("allowed_source_agents"),
            normalized_binding["from_agent"],
            fold=True,
        ) or not _allowed(
            record.get("allowed_source_instances"),
            normalized_binding["from_instance"],
        ) or not _allowed(
            record.get("allowed_target_agents"),
            normalized_binding["to_agent"],
            fold=True,
        ) or not _allowed(
            record.get("allowed_target_instances"),
            normalized_binding["to_instance"],
        ):
            results.append(_failure(credential_id, "invalid", proof_id=proof_id))
            continue
        try:
            allowed_resources = _clean_resources(record.get("allowed_resources"))
        except ValueError:
            results.append(_failure(credential_id, "invalid", proof_id=proof_id))
            continue
        if allowed_resources and not set(normalized_binding["resources"]).issubset(
            allowed_resources
        ):
            results.append(_failure(credential_id, "invalid", proof_id=proof_id))
            continue
        try:
            secret = _credential_secret(record)
        except ValueError:
            results.append(_failure(credential_id, "invalid", proof_id=proof_id))
            continue
        expected = hmac.new(
            secret.encode("utf-8"), _canonical_json(claims), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, digest):
            results.append(_failure(credential_id, "invalid", proof_id=proof_id))
            continue
        binding_sha256 = hashlib.sha256(
            _canonical_json(normalized_binding)
        ).hexdigest()
        proof_sha256 = hashlib.sha256(_canonical_json(dict(raw))).hexdigest()
        replay_state = nonce_store.remember(
            credential_id=credential_id,
            nonce=nonce,
            message_id=normalized_binding["message_id"],
            binding_sha256=binding_sha256,
            proof_sha256=proof_sha256,
            expires_at=expires_at,
            now=timestamp,
        )
        if replay_state == "replay":
            results.append(_failure(credential_id, "invalid", proof_id=proof_id))
            continue
        configured_scopes = record.get("scopes")
        if not isinstance(configured_scopes, (list, tuple, set, frozenset)):
            results.append(_failure(credential_id, "invalid", proof_id=proof_id))
            continue
        try:
            scopes = sorted(
                {
                    _clean_identifier(scope, field="scope", max_length=128)
                    for scope in configured_scopes
                }
            )
        except ValueError:
            results.append(_failure(credential_id, "invalid", proof_id=proof_id))
            continue
        if not scopes:
            results.append(_failure(credential_id, "invalid", proof_id=proof_id))
            continue
        result = {
            "credential_id": credential_id,
            "group": str(record.get("group") or credential_id),
            "state": "success",
            "verification": "runtime_verified",
            "scopes": scopes,
            "resources": list(normalized_binding["resources"]),
            "proof_id": proof_id,
            "expires_at": expires_at,
        }
        if replay_state == "same":
            result["idempotent_revalidation"] = True
        results.append(result)
    return results


def default_nonce_store(hashi_root: Path | str) -> PrivateAuthorizationStore:
    return PrivateAuthorizationStore(
        Path(hashi_root) / "state" / "private_authorization_nonces.sqlite3"
    )


def verify_configured_proofs(
    hashi_root: Path | str,
    *,
    proofs: Iterable[Mapping[str, Any]],
    binding: Mapping[str, Any],
    now: float | None = None,
) -> list[dict[str, Any]]:
    return verify_private_authorization_proofs(
        load_private_authorization_config(hashi_root),
        proofs=proofs,
        binding=binding,
        nonce_store=default_nonce_store(hashi_root),
        now=now,
    )


def build_configured_proofs(
    hashi_root: Path | str,
    *,
    credential_ids: Iterable[str],
    binding: Mapping[str, Any],
    now: float | None = None,
) -> list[dict[str, Any]]:
    return build_private_authorization_proofs(
        load_private_authorization_config(hashi_root),
        credential_ids=credential_ids,
        binding=binding,
        now=now,
    )


def public_private_authorization_capabilities() -> dict[str, Any]:
    return json.loads(json.dumps(PRIVATE_AUTHORIZATION_CAPABILITIES))


__all__ = [
    "DEFAULT_PROOF_TTL_SECONDS",
    "MAX_PROOF_TTL_SECONDS",
    "PRIVATE_AUTHORIZATION_CONFIG_KEY",
    "PRIVATE_AUTHORIZATION_CAPABILITIES",
    "PRIVATE_AUTHORIZATION_TYPE",
    "PRIVATE_AUTHORIZATION_VERSION",
    "PrivateAuthorizationStore",
    "authorization_content_sha256",
    "build_configured_proofs",
    "build_private_authorization_proofs",
    "default_nonce_store",
    "load_private_authorization_config",
    "normalize_authorization_binding",
    "public_private_authorization_capabilities",
    "verify_configured_proofs",
    "verify_private_authorization_proofs",
]
