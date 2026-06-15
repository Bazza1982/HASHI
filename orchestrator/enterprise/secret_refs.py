from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class ResolvedSecret:
    ref: str
    value: str
    source: str

    def redacted(self) -> dict:
        return {"ref": self.ref, "source": self.source, "value": "[REDACTED]"}


class ConnectorSecretResolver:
    def __init__(self, *, secrets: Mapping[str, str] | None = None, environ: Mapping[str, str] | None = None):
        self.secrets = {str(key): str(value) for key, value in dict(secrets or {}).items()}
        self.environ = environ if environ is not None else os.environ

    def resolve(self, secret_ref: str) -> ResolvedSecret:
        ref = str(secret_ref or "").strip()
        if not ref:
            raise ValueError("secret_ref is required")
        if ref.startswith("env://"):
            return self._resolve_env(ref, ref.removeprefix("env://"))
        if ref.startswith("env:"):
            return self._resolve_env(ref, ref.removeprefix("env:"))
        if ref.startswith("secrets://"):
            return self._resolve_hashi_secret(ref, ref.removeprefix("secrets://"))
        if ref.startswith("hashi://"):
            return self._resolve_hashi_secret(ref, ref.removeprefix("hashi://"))
        if ref.startswith("vault://"):
            raise ValueError("vault secret resolver is not configured")
        raise ValueError(f"unsupported secret_ref scheme: {ref}")

    def _resolve_env(self, ref: str, name: str) -> ResolvedSecret:
        key = _require_ref_key(name, "environment variable")
        value = self.environ.get(key)
        if value is None or value == "":
            raise ValueError(f"environment secret is not set: {key}")
        return ResolvedSecret(ref=ref, value=str(value), source="env")

    def _resolve_hashi_secret(self, ref: str, name: str) -> ResolvedSecret:
        key = _require_ref_key(name, "HASHI secret")
        value = self.secrets.get(key)
        if value is None or value == "":
            raise ValueError(f"HASHI secret is not set: {key}")
        return ResolvedSecret(ref=ref, value=str(value), source="hashi")


def _require_ref_key(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{label} reference is required")
    return normalized
