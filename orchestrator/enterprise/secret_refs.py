from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


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
