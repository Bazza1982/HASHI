"""Application-layer confidentiality for HASHI Agent move packages."""

from __future__ import annotations

import hashlib
import os

from .package import AgentMoveError

ENVELOPE_SCHEME = "aes-256-gcm-v1"
_MAGIC = b"HASHI-MOVE-V1\x00"
_SALT_BYTES = 16
_NONCE_BYTES = 12
_TAG_BYTES = 16
ENVELOPE_OVERHEAD_BYTES = len(_MAGIC) + _SALT_BYTES + _NONCE_BYTES + _TAG_BYTES


def encrypted_package_size(plaintext_bytes: int) -> int:
    return max(0, int(plaintext_bytes)) + ENVELOPE_OVERHEAD_BYTES


def encrypt_package_transport(
    package_bytes: bytes,
    *,
    shared_token: str,
    source_instance: str,
    target_instance: str,
    package_sha256: str,
) -> bytes:
    """Encrypt one package and bind it to both peers and its cleartext digest."""

    if not package_bytes:
        raise AgentMoveError("Agent move package is empty")
    salt = os.urandom(_SALT_BYTES)
    nonce = os.urandom(_NONCE_BYTES)
    key = _derive_key(shared_token, salt)
    ciphertext = _aesgcm(key).encrypt(
        nonce,
        package_bytes,
        _associated_data(source_instance, target_instance, package_sha256),
    )
    return _MAGIC + salt + nonce + ciphertext


def decrypt_package_transport(
    envelope: bytes,
    *,
    shared_token: str,
    source_instance: str,
    target_instance: str,
    package_sha256: str,
) -> bytes:
    """Authenticate and decrypt one package transport envelope."""

    minimum = ENVELOPE_OVERHEAD_BYTES + 1
    if len(envelope) < minimum or not envelope.startswith(_MAGIC):
        raise AgentMoveError("Agent move transport envelope is invalid")
    offset = len(_MAGIC)
    salt = envelope[offset : offset + _SALT_BYTES]
    offset += _SALT_BYTES
    nonce = envelope[offset : offset + _NONCE_BYTES]
    ciphertext = envelope[offset + _NONCE_BYTES :]
    try:
        plaintext = _aesgcm(_derive_key(shared_token, salt)).decrypt(
            nonce,
            ciphertext,
            _associated_data(source_instance, target_instance, package_sha256),
        )
    except Exception as exc:
        raise AgentMoveError(
            "Agent move transport envelope authentication failed"
        ) from exc
    if hashlib.sha256(plaintext).hexdigest() != str(package_sha256 or "").lower():
        raise AgentMoveError("Agent move transport plaintext SHA-256 does not match")
    return plaintext


def _derive_key(shared_token: str, salt: bytes) -> bytes:
    token = str(shared_token or "").encode("utf-8")
    if not token:
        raise AgentMoveError("HASHI Remote shared token is required")
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as exc:  # pragma: no cover - a required runtime dependency
        raise AgentMoveError(
            "cryptography is required for Agent move transport encryption"
        ) from exc
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"HASHI agent-move-v1 transport envelope",
    ).derive(token)


def _aesgcm(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - a required runtime dependency
        raise AgentMoveError(
            "cryptography is required for Agent move transport encryption"
        ) from exc
    return AESGCM(key)


def _associated_data(
    source_instance: str, target_instance: str, package_sha256: str
) -> bytes:
    source = str(source_instance or "").strip().upper()
    target = str(target_instance or "").strip().upper()
    digest = str(package_sha256 or "").strip().lower()
    if not source or not target or len(digest) != 64:
        raise AgentMoveError("Agent move transport binding is invalid")
    return f"{ENVELOPE_SCHEME}\n{source}\n{target}\n{digest}".encode("utf-8")
