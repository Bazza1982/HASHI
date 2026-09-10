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


STREAM_CHUNK_BYTES = 1024 * 1024


def encrypt_package_file(source, destination, **binding) -> None:
    """Write the existing AES-GCM envelope with bounded memory."""
    from pathlib import Path
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    salt, nonce = os.urandom(_SALT_BYTES), os.urandom(_NONCE_BYTES)
    encryptor = Cipher(algorithms.AES(_derive_key(binding["shared_token"], salt)),
                       modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(_associated_data(
        binding["source_instance"], binding["target_instance"], binding["package_sha256"]))
    digest = hashlib.sha256()
    output = Path(destination)
    writer = output.open("xb")
    try:
        with writer, Path(source).open("rb") as reader:
            os.chmod(output, 0o600)
            writer.write(_MAGIC + salt + nonce)
            while chunk := reader.read(STREAM_CHUNK_BYTES):
                digest.update(chunk)
                writer.write(encryptor.update(chunk))
            writer.write(encryptor.finalize())
            writer.write(encryptor.tag)
        if digest.hexdigest() != binding["package_sha256"].lower():
            raise AgentMoveError("Agent move package changed during encryption")
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def decrypt_package_file(source, destination, *, max_plaintext_bytes: int, **binding) -> None:
    """Publish plaintext only after the complete GCM tag and digest validate."""
    from pathlib import Path
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    source, destination = Path(source), Path(destination)
    size = source.stat().st_size
    if size <= ENVELOPE_OVERHEAD_BYTES or size > max_plaintext_bytes + ENVELOPE_OVERHEAD_BYTES:
        raise AgentMoveError("Agent move transport envelope size is invalid")
    temporary = destination.with_name(destination.name + ".unauthenticated")
    writer = temporary.open("xb")
    try:
        with writer, source.open("rb") as reader:
            os.chmod(temporary, 0o600)
            if reader.read(len(_MAGIC)) != _MAGIC:
                raise AgentMoveError("Agent move transport envelope is invalid")
            salt, nonce = reader.read(_SALT_BYTES), reader.read(_NONCE_BYTES)
            reader.seek(-_TAG_BYTES, os.SEEK_END)
            tag = reader.read(_TAG_BYTES)
            reader.seek(len(_MAGIC) + _SALT_BYTES + _NONCE_BYTES)
            decryptor = Cipher(algorithms.AES(_derive_key(binding["shared_token"], salt)),
                               modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(_associated_data(
                binding["source_instance"], binding["target_instance"], binding["package_sha256"]))
            digest = hashlib.sha256()
            remaining = size - ENVELOPE_OVERHEAD_BYTES
            while remaining:
                chunk = reader.read(min(STREAM_CHUNK_BYTES, remaining))
                if not chunk:
                    raise AgentMoveError("Agent move transport envelope was truncated")
                remaining -= len(chunk)
                plaintext = decryptor.update(chunk)
                digest.update(plaintext)
                writer.write(plaintext)
            plaintext = decryptor.finalize()
            digest.update(plaintext)
            writer.write(plaintext)
            if digest.hexdigest() != binding["package_sha256"].lower():
                raise AgentMoveError("Agent move transport plaintext SHA-256 does not match")
        os.replace(temporary, destination)
    except BaseException as exc:
        temporary.unlink(missing_ok=True)
        if isinstance(exc, (KeyboardInterrupt, SystemExit, AgentMoveError, OSError)):
            raise
        raise AgentMoveError("Agent move transport envelope authentication failed") from exc
