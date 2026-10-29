"""Verify immutable decision claims from an explicitly pinned approval issuer.

This code holds only public verification keys. A valid signature proves that
the corresponding private key signed these exact claims, not that a human was
authenticated. The private signing key, issuer, and human-authentication path
must be independently protected before this verifier is enabled in a runtime.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import fields
import json
import math
from typing import Mapping
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from orchestrator.permissions.authority import DecisionReceipt


_DOMAIN = b"hashi-permission-decision-v1\0"
_KEY_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_BASE64_URL = re.compile(r"[A-Za-z0-9_-]+\Z")
_CLAIMS = frozenset(field.name for field in fields(DecisionReceipt))
_INT_FIELDS = frozenset({"request_revision", "policy_revision", "duration_seconds"})
_OPTIONAL_STRINGS = frozenset({"operation_id", "arguments_digest"})
_STRINGS = _CLAIMS - _INT_FIELDS - _OPTIONAL_STRINGS - {"expires_at"}
_MAX_REFERENCE = 16 * 1024


def _decode_segment(segment: str) -> bytes:
    if not _BASE64_URL.fullmatch(segment):
        raise ValueError("invalid encoded segment")
    decoded = base64.b64decode(
        segment + "=" * (-len(segment) % 4), altchars=b"-_", validate=True
    )
    if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != segment:
        raise ValueError("non-canonical encoded segment")
    return decoded


def _decode_claims(raw: bytes) -> DecisionReceipt:
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict) or frozenset(parsed) != _CLAIMS:
        raise ValueError("unexpected decision claims")
    canonical = json.dumps(
        parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    if canonical != raw:
        raise ValueError("decision claims are not canonical")
    if any(not isinstance(parsed[key], str) or not parsed[key] for key in _STRINGS):
        raise ValueError("invalid string claim")
    if any(type(parsed[key]) is not int for key in _INT_FIELDS):
        raise ValueError("invalid integer claim")
    if any(parsed[key] is not None and not isinstance(parsed[key], str) for key in _OPTIONAL_STRINGS):
        raise ValueError("invalid optional claim")
    expiry = parsed["expires_at"]
    if isinstance(expiry, bool) or not isinstance(expiry, (float, int)) or not math.isfinite(expiry):
        raise ValueError("invalid expiry")
    return DecisionReceipt(**parsed)


class SignedReceiptVerifier:
    """Accept only exact canonical claims signed by a configured public key."""

    def __init__(self, trusted_keys: Mapping[str, bytes]) -> None:
        if not isinstance(trusted_keys, Mapping) or not trusted_keys:
            raise ValueError("at least one trusted public key is required")
        keys: dict[str, Ed25519PublicKey] = {}
        for key_id, raw_key in trusted_keys.items():
            if (not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id)
                    or not isinstance(raw_key, bytes) or len(raw_key) != 32):
                raise ValueError("invalid trusted public key")
            keys[key_id] = Ed25519PublicKey.from_public_bytes(raw_key)
        self._keys = keys

    def verify(self, reference: str) -> DecisionReceipt | None:
        if not isinstance(reference, str) or len(reference) > _MAX_REFERENCE:
            return None
        segments = reference.split(".")
        if len(segments) != 4 or segments[0] != "hpd1":
            return None
        _, key_id, encoded_payload, encoded_signature = segments
        key = self._keys.get(key_id)
        if key is None:
            return None
        try:
            payload = _decode_segment(encoded_payload)
            signature = _decode_segment(encoded_signature)
            if len(signature) != 64:
                return None
            key.verify(signature, _DOMAIN + key_id.encode("ascii") + b"\0" + payload)
            return _decode_claims(payload)
        except (ValueError, TypeError, UnicodeError, binascii.Error, InvalidSignature, RecursionError):
            return None
