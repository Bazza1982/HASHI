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

from orchestrator.permissions.authority import DecisionReceipt, OperationOutcomeReceipt, ResourceStopReceipt


_DOMAIN = b"hashi-permission-decision-v1\0"
_OUTCOME_DOMAIN = b"hashi-permission-outcome-v1\0"
_STOP_DOMAIN = b"hashi-permission-stop-v1\0"
_KEY_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_BASE64_URL = re.compile(r"[A-Za-z0-9_-]+\Z")
_CLAIMS = frozenset(field.name for field in fields(DecisionReceipt))
_OUTCOME_CLAIMS = frozenset(field.name for field in fields(OperationOutcomeReceipt))
_STOP_CLAIMS = frozenset(field.name for field in fields(ResourceStopReceipt))
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


def _decode_object(raw: bytes, expected: frozenset[str]) -> dict:
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict) or frozenset(parsed) != expected:
        raise ValueError("unexpected receipt claims")
    canonical = json.dumps(
        parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    if canonical != raw:
        raise ValueError("receipt claims are not canonical")
    return parsed


def _valid_expiry(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _decode_claims(raw: bytes) -> DecisionReceipt:
    parsed = _decode_object(raw, _CLAIMS)
    if any(not isinstance(parsed[key], str) or not parsed[key] for key in _STRINGS):
        raise ValueError("invalid string claim")
    if any(type(parsed[key]) is not int for key in _INT_FIELDS):
        raise ValueError("invalid integer claim")
    if any(parsed[key] is not None and not isinstance(parsed[key], str) for key in _OPTIONAL_STRINGS):
        raise ValueError("invalid optional claim")
    if not _valid_expiry(parsed["expires_at"]):
        raise ValueError("invalid expiry")
    return DecisionReceipt(**parsed)


def _supervisor_claims(raw: bytes, expected: frozenset[str]) -> dict:
    parsed = _decode_object(raw, expected)
    if any(not isinstance(parsed[key], str) or not parsed[key] for key in expected - {"expires_at"}):
        raise ValueError("invalid supervisor claim")
    if not _valid_expiry(parsed["expires_at"]):
        raise ValueError("invalid expiry")
    return parsed


class SignedReceiptVerifier:
    """Verify decisions and outcomes with separate, pinned issuer keys."""

    def __init__(
        self, trusted_keys: Mapping[str, bytes], *,
        trusted_supervisor_keys: Mapping[str, bytes] | None = None,
    ) -> None:
        self._keys, decision_bytes = self._load_keys(trusted_keys)
        if trusted_supervisor_keys is None:
            self._supervisor_keys: dict[str, Ed25519PublicKey] = {}
        else:
            self._supervisor_keys, supervisor_bytes = self._load_keys(trusted_supervisor_keys)
            if decision_bytes & supervisor_bytes:
                raise ValueError("approval and supervisor signing keys must be distinct")

    @staticmethod
    def _load_keys(trusted: Mapping[str, bytes]) -> tuple[dict[str, Ed25519PublicKey], set[bytes]]:
        if not isinstance(trusted, Mapping) or not trusted:
            raise ValueError("at least one trusted public key is required")
        keys: dict[str, Ed25519PublicKey] = {}
        raw_keys: set[bytes] = set()
        for key_id, raw_key in trusted.items():
            if (not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id)
                    or not isinstance(raw_key, bytes) or len(raw_key) != 32):
                raise ValueError("invalid trusted public key")
            if raw_key in raw_keys:
                raise ValueError("one public key cannot identify multiple issuers")
            keys[key_id] = Ed25519PublicKey.from_public_bytes(raw_key)
            raw_keys.add(raw_key)
        return keys, raw_keys

    @staticmethod
    def _verified_payload(
        reference: str, *, prefix: str, domain: bytes, keys: Mapping[str, Ed25519PublicKey],
    ) -> tuple[bytes, str] | None:
        if not isinstance(reference, str) or len(reference) > _MAX_REFERENCE:
            return None
        segments = reference.split(".")
        if len(segments) != 4 or segments[0] != prefix:
            return None
        _, key_id, encoded_payload, encoded_signature = segments
        key = keys.get(key_id)
        if key is None:
            return None
        try:
            payload = _decode_segment(encoded_payload)
            signature = _decode_segment(encoded_signature)
            if len(signature) != 64:
                return None
            key.verify(signature, domain + key_id.encode("ascii") + b"\0" + payload)
            return payload, key_id
        except (ValueError, TypeError, UnicodeError, binascii.Error, InvalidSignature, RecursionError):
            return None

    def verify(self, reference: str) -> DecisionReceipt | None:
        verified = self._verified_payload(reference, prefix="hpd1", domain=_DOMAIN, keys=self._keys)
        if verified is None:
            return None
        payload, _ = verified
        try:
            return _decode_claims(payload)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return None

    def verify_outcome(self, reference: str) -> OperationOutcomeReceipt | None:
        verified = self._verified_payload(
            reference, prefix="hpo1", domain=_OUTCOME_DOMAIN, keys=self._supervisor_keys,
        )
        if verified is None:
            return None
        payload, key_id = verified
        try:
            receipt = OperationOutcomeReceipt(**_supervisor_claims(payload, _OUTCOME_CLAIMS))
            return receipt if receipt.observer_id == key_id else None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return None

    def verify_stop(self, reference: str) -> ResourceStopReceipt | None:
        verified = self._verified_payload(
            reference, prefix="hps1", domain=_STOP_DOMAIN, keys=self._supervisor_keys,
        )
        if verified is None:
            return None
        payload, key_id = verified
        try:
            receipt = ResourceStopReceipt(**_supervisor_claims(payload, _STOP_CLAIMS))
            return receipt if receipt.observer_id == key_id else None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return None
