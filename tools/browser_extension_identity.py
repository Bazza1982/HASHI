"""Create a stable, instance-scoped Chrome extension identity."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from orchestrator.file_permissions import tighten_fd_permissions


_NAMESPACE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,95}$")


class BrowserExtensionIdentityError(RuntimeError):
    """An extension identity file is missing, malformed, or wrongly scoped."""


def extension_id_for_public_key(public_key_der: bytes) -> str:
    """Return Chrome's 32-letter extension ID for a DER public key."""

    digest = hashlib.sha256(public_key_der).digest()[:16]
    alphabet = "abcdefghijklmnop"
    return "".join(
        alphabet[nibble]
        for byte in digest
        for nibble in (byte >> 4, byte & 0x0F)
    )


def _validate_public_key(encoded: str) -> bytes:
    try:
        der = base64.b64decode(encoded, validate=True)
        key = serialization.load_der_public_key(der)
    except Exception as exc:
        raise BrowserExtensionIdentityError(
            "browser extension public key is malformed"
        ) from exc
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
        raise BrowserExtensionIdentityError(
            "browser extension identity requires an RSA-2048-or-stronger key"
        )
    return der


def _write_once(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    try:
        tighten_fd_permissions(descriptor)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(content + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        finally:
            Path(temporary).unlink(missing_ok=True)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


def load_or_create_identity(state_dir: Path, namespace: str) -> dict[str, str]:
    normalized = str(namespace or "").strip().casefold()
    if not _NAMESPACE.fullmatch(normalized):
        raise BrowserExtensionIdentityError("browser extension namespace is invalid")
    path = Path(state_dir) / "extension-public-key.txt"
    if not path.exists():
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        der = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        _write_once(path, base64.b64encode(der).decode("ascii"))
    try:
        encoded = path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise BrowserExtensionIdentityError(
            f"browser extension identity is unavailable: {path}"
        ) from exc
    der = _validate_public_key(encoded)
    return {
        "namespace": normalized,
        "manifest_key": encoded,
        "extension_id": extension_id_for_public_key(der),
        "key_path": str(path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--namespace", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(
        json.dumps(
            load_or_create_identity(Path(args.state_dir), args.namespace),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
