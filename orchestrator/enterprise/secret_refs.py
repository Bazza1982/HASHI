from __future__ import annotations

import os
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib import request as urllib_request


@dataclass(frozen=True)
class ResolvedSecret:
    ref: str
    value: str
    source: str

    def redacted(self) -> dict:
        return {"ref": self.ref, "source": self.source, "value": "[REDACTED]"}


class SecretProvider:
    schemes: tuple[str, ...] = ()

    def supports(self, ref: str) -> bool:
        return any(ref.startswith(f"{scheme}://") or ref.startswith(f"{scheme}:") for scheme in self.schemes)

    def resolve(self, ref: str) -> ResolvedSecret:
        raise NotImplementedError


class EnvSecretProvider(SecretProvider):
    schemes = ("env",)

    def __init__(self, *, environ: Mapping[str, str] | None = None):
        self.environ = environ if environ is not None else os.environ

    def resolve(self, ref: str) -> ResolvedSecret:
        name = ref.removeprefix("env://").removeprefix("env:")
        key = _require_ref_key(name, "environment variable")
        value = self.environ.get(key)
        if value is None or value == "":
            raise ValueError(f"environment secret is not set: {key}")
        return ResolvedSecret(ref=ref, value=str(value), source="env")


class HashiSecretProvider(SecretProvider):
    schemes = ("secrets", "hashi")

    def __init__(self, *, secrets: Mapping[str, str] | None = None):
        self.secrets = {str(key): str(value) for key, value in dict(secrets or {}).items()}

    def resolve(self, ref: str) -> ResolvedSecret:
        name = ref.removeprefix("secrets://").removeprefix("hashi://")
        key = _require_ref_key(name, "HASHI secret")
        value = self.secrets.get(key)
        if value is None or value == "":
            raise ValueError(f"HASHI secret is not set: {key}")
        return ResolvedSecret(ref=ref, value=str(value), source="hashi")


class FileSecretProvider(SecretProvider):
    schemes = ("file",)

    def __init__(self, *, root: Path | str):
        self.root = Path(root).resolve()

    def resolve(self, ref: str) -> ResolvedSecret:
        name = ref.removeprefix("file://")
        relative_path = _require_ref_key(name, "file secret")
        path = (self.root / relative_path).resolve()
        if self.root not in (path, *path.parents):
            raise ValueError("file secret path escapes configured root")
        if not path.is_file():
            raise ValueError(f"file secret is not set: {relative_path}")
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"file secret is empty: {relative_path}")
        return ResolvedSecret(ref=ref, value=value, source="file")


class KubernetesMountedSecretProvider(SecretProvider):
    schemes = ("k8s",)

    def __init__(self, *, root: Path | str):
        self.root = Path(root).resolve()

    def resolve(self, ref: str) -> ResolvedSecret:
        name = ref.removeprefix("k8s://")
        parts = [_require_ref_key(part, "kubernetes secret") for part in name.split("/")]
        if len(parts) != 3:
            raise ValueError("k8s secret ref must be k8s://namespace/name/key")
        namespace, secret_name, key = parts
        path = (self.root / namespace / secret_name / key).resolve()
        if self.root not in (path, *path.parents):
            raise ValueError("k8s secret path escapes configured root")
        if not path.is_file():
            raise ValueError(f"k8s mounted secret is not set: {namespace}/{secret_name}/{key}")
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"k8s mounted secret is empty: {namespace}/{secret_name}/{key}")
        return ResolvedSecret(ref=ref, value=value, source="k8s")


VaultClient = Callable[[str, str], dict[str, Any]]


class VaultSecretProvider(SecretProvider):
    schemes = ("vault",)

    def __init__(
        self,
        *,
        address: str,
        token: str,
        client: VaultClient | None = None,
        timeout: float = 10.0,
    ):
        self.address = str(address or "").strip().rstrip("/")
        self.token = str(token or "").strip()
        self.client = client
        self.timeout = float(timeout)
        if not self.address:
            raise ValueError("vault address is required")
        if not self.token:
            raise ValueError("vault token is required")

    def resolve(self, ref: str) -> ResolvedSecret:
        path, field = _split_vault_ref(ref.removeprefix("vault://"))
        payload = (self.client or self._default_client)(path, self.token)
        value = _extract_vault_value(payload, field)
        if value == "":
            raise ValueError(f"vault secret field is empty: {path}#{field}")
        return ResolvedSecret(ref=ref, value=value, source="vault")

    def _default_client(self, path: str, token: str) -> dict[str, Any]:
        url = f"{self.address}/v1/{path}"
        req = urllib_request.Request(url, headers={"X-Vault-Token": token}, method="GET")
        with urllib_request.urlopen(req, timeout=self.timeout) as response:
            try:
                payload = json.loads(response.read().decode("utf-8"))
            except Exception as exc:
                raise ValueError("vault response is invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("vault response must be a JSON object")
        return payload


class ConnectorSecretResolver:
    def __init__(
        self,
        *,
        secrets: Mapping[str, str] | None = None,
        environ: Mapping[str, str] | None = None,
        providers: list[SecretProvider] | tuple[SecretProvider, ...] | None = None,
    ):
        self.secrets = {str(key): str(value) for key, value in dict(secrets or {}).items()}
        self.environ = environ if environ is not None else os.environ
        self.providers = [
            *(providers or ()),
            EnvSecretProvider(environ=self.environ),
            HashiSecretProvider(secrets=self.secrets),
        ]

    def resolve(self, secret_ref: str) -> ResolvedSecret:
        ref = str(secret_ref or "").strip()
        if not ref:
            raise ValueError("secret_ref is required")
        for provider in self.providers:
            if provider.supports(ref):
                return provider.resolve(ref)
        if ref.startswith("vault://"):
            raise ValueError("vault secret resolver is not configured")
        if ref.startswith("k8s://"):
            raise ValueError("kubernetes secret resolver is not configured")
        raise ValueError(f"unsupported secret_ref scheme: {ref}")


def _require_ref_key(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{label} reference is required")
    return normalized


def _split_vault_ref(value: str) -> tuple[str, str]:
    raw = _require_ref_key(value, "vault secret")
    if "#" in raw:
        path, field = raw.rsplit("#", 1)
    else:
        path, field = raw, "value"
    return _require_ref_key(path, "vault path"), _require_ref_key(field, "vault field")


def _extract_vault_value(payload: dict[str, Any], field: str) -> str:
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict):
        raise ValueError("vault response missing data object")
    value = data.get(field)
    if value is None:
        raise ValueError(f"vault secret field is not set: {field}")
    return str(value)
