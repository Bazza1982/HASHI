from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol


class KubernetesLeaseConflict(Exception):
    """Raised by clients when a Kubernetes Lease write loses a resourceVersion race."""


@dataclass(frozen=True)
class KubernetesLease:
    namespace: str
    name: str
    holder_identity: str
    lease_duration_seconds: int
    acquire_time: str
    renew_time: str
    resource_version: str | None = None

    @property
    def expires_at(self) -> datetime:
        return _parse_ts(self.renew_time) + timedelta(seconds=max(1, int(self.lease_duration_seconds)))

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at <= _normalize_now(now)


@dataclass(frozen=True)
class KubernetesLeaseAttempt:
    acquired: bool
    lease: KubernetesLease | None
    current_holder_id: str | None = None
    conflict: bool = False


class KubernetesLeaseClient(Protocol):
    def get_lease(self, namespace: str, name: str) -> KubernetesLease | None:
        ...

    def create_lease(self, lease: KubernetesLease) -> KubernetesLease:
        ...

    def replace_lease(self, lease: KubernetesLease) -> KubernetesLease:
        ...

    def delete_lease(self, namespace: str, name: str, *, holder_identity: str) -> bool:
        ...


class KubernetesLeaseCoordinator:
    def __init__(self, client: KubernetesLeaseClient, *, namespace: str):
        self.client = client
        self.namespace = _require_text(namespace, "namespace")

    def acquire(
        self,
        name: str,
        *,
        holder_identity: str,
        ttl_seconds: int | float = 60,
        now: datetime | None = None,
    ) -> KubernetesLeaseAttempt:
        lease_name = _require_text(name, "lease name")
        holder = _require_text(holder_identity, "holder identity")
        issued_at = _normalize_now(now)
        duration = max(1, int(ttl_seconds or 60))
        current = self.client.get_lease(self.namespace, lease_name)
        if current is not None and not current.is_expired(issued_at) and current.holder_identity != holder:
            return KubernetesLeaseAttempt(
                acquired=False,
                lease=current,
                current_holder_id=current.holder_identity,
            )

        acquire_time = current.acquire_time if current and current.holder_identity == holder else _format_ts(issued_at)
        next_lease = KubernetesLease(
            namespace=self.namespace,
            name=lease_name,
            holder_identity=holder,
            lease_duration_seconds=duration,
            acquire_time=acquire_time,
            renew_time=_format_ts(issued_at),
            resource_version=current.resource_version if current else None,
        )
        try:
            if current is None:
                written = self.client.create_lease(next_lease)
            else:
                written = self.client.replace_lease(next_lease)
        except KubernetesLeaseConflict:
            latest = self.client.get_lease(self.namespace, lease_name)
            return KubernetesLeaseAttempt(
                acquired=False,
                lease=latest,
                current_holder_id=latest.holder_identity if latest else None,
                conflict=True,
            )
        return KubernetesLeaseAttempt(acquired=True, lease=written, current_holder_id=holder)

    def renew(
        self,
        name: str,
        *,
        holder_identity: str,
        ttl_seconds: int | float = 60,
        now: datetime | None = None,
    ) -> KubernetesLeaseAttempt:
        lease_name = _require_text(name, "lease name")
        holder = _require_text(holder_identity, "holder identity")
        issued_at = _normalize_now(now)
        current = self.client.get_lease(self.namespace, lease_name)
        if current is None:
            return KubernetesLeaseAttempt(acquired=False, lease=None, current_holder_id=None)
        if current.holder_identity != holder or current.is_expired(issued_at):
            return KubernetesLeaseAttempt(
                acquired=False,
                lease=current,
                current_holder_id=current.holder_identity,
            )
        next_lease = KubernetesLease(
            namespace=self.namespace,
            name=lease_name,
            holder_identity=holder,
            lease_duration_seconds=max(1, int(ttl_seconds or current.lease_duration_seconds)),
            acquire_time=current.acquire_time,
            renew_time=_format_ts(issued_at),
            resource_version=current.resource_version,
        )
        try:
            written = self.client.replace_lease(next_lease)
        except KubernetesLeaseConflict:
            latest = self.client.get_lease(self.namespace, lease_name)
            return KubernetesLeaseAttempt(
                acquired=False,
                lease=latest,
                current_holder_id=latest.holder_identity if latest else None,
                conflict=True,
            )
        return KubernetesLeaseAttempt(acquired=True, lease=written, current_holder_id=holder)

    def release(self, name: str, *, holder_identity: str) -> bool:
        lease_name = _require_text(name, "lease name")
        holder = _require_text(holder_identity, "holder identity")
        return self.client.delete_lease(self.namespace, lease_name, holder_identity=holder)


class KubernetesApiLeaseClient:
    group = "coordination.k8s.io"
    version = "v1"
    plural = "leases"

    def __init__(self, api: Any):
        self.api = api

    @classmethod
    def from_config(cls, *, in_cluster: bool = True, kubeconfig_path: str | None = None) -> "KubernetesApiLeaseClient":
        try:
            from kubernetes import client, config
        except ImportError as exc:
            raise RuntimeError("kubernetes package is required for KubernetesApiLeaseClient.from_config()") from exc

        if in_cluster:
            config.load_incluster_config()
        else:
            config.load_kube_config(config_file=kubeconfig_path)
        return cls(client.CustomObjectsApi())

    def get_lease(self, namespace: str, name: str) -> KubernetesLease | None:
        try:
            body = self.api.get_namespaced_custom_object(
                self.group,
                self.version,
                _require_text(namespace, "namespace"),
                self.plural,
                _require_text(name, "lease name"),
            )
        except Exception as exc:
            if _api_status(exc) == 404:
                return None
            raise
        return _lease_from_body(body)

    def create_lease(self, lease: KubernetesLease) -> KubernetesLease:
        try:
            body = self.api.create_namespaced_custom_object(
                self.group,
                self.version,
                lease.namespace,
                self.plural,
                _lease_to_body(lease, include_resource_version=False),
            )
        except Exception as exc:
            if _api_status(exc) == 409:
                raise KubernetesLeaseConflict() from exc
            raise
        return _lease_from_body(body)

    def replace_lease(self, lease: KubernetesLease) -> KubernetesLease:
        try:
            body = self.api.replace_namespaced_custom_object(
                self.group,
                self.version,
                lease.namespace,
                self.plural,
                lease.name,
                _lease_to_body(lease, include_resource_version=True),
            )
        except Exception as exc:
            if _api_status(exc) == 409:
                raise KubernetesLeaseConflict() from exc
            raise
        return _lease_from_body(body)

    def delete_lease(self, namespace: str, name: str, *, holder_identity: str) -> bool:
        lease = self.get_lease(namespace, name)
        if lease is None or lease.holder_identity != _require_text(holder_identity, "holder identity"):
            return False
        try:
            self.api.delete_namespaced_custom_object(
                self.group,
                self.version,
                _require_text(namespace, "namespace"),
                self.plural,
                _require_text(name, "lease name"),
            )
        except Exception as exc:
            if _api_status(exc) == 404:
                return False
            raise
        return True


def _require_text(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required")
    return text


def _normalize_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_ts(value: datetime) -> str:
    return _normalize_now(value).isoformat().replace("+00:00", "Z")


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _lease_to_body(lease: KubernetesLease, *, include_resource_version: bool) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "name": lease.name,
        "namespace": lease.namespace,
    }
    if include_resource_version and lease.resource_version:
        metadata["resourceVersion"] = lease.resource_version
    return {
        "apiVersion": f"{KubernetesApiLeaseClient.group}/{KubernetesApiLeaseClient.version}",
        "kind": "Lease",
        "metadata": metadata,
        "spec": {
            "holderIdentity": lease.holder_identity,
            "leaseDurationSeconds": max(1, int(lease.lease_duration_seconds)),
            "acquireTime": lease.acquire_time,
            "renewTime": lease.renew_time,
        },
    }


def _lease_from_body(body: dict[str, Any]) -> KubernetesLease:
    metadata = body.get("metadata") or {}
    spec = body.get("spec") or {}
    return KubernetesLease(
        namespace=_require_text(metadata.get("namespace"), "namespace"),
        name=_require_text(metadata.get("name"), "lease name"),
        holder_identity=_require_text(spec.get("holderIdentity"), "holder identity"),
        lease_duration_seconds=max(1, int(spec.get("leaseDurationSeconds") or 1)),
        acquire_time=_require_text(spec.get("acquireTime"), "acquire time"),
        renew_time=_require_text(spec.get("renewTime"), "renew time"),
        resource_version=metadata.get("resourceVersion"),
    )


def _api_status(exc: Exception) -> int | None:
    status = getattr(exc, "status", None)
    if status is None:
        return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None
