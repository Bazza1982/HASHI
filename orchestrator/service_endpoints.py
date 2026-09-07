"""Core-owned publication and discovery for live HASHI service endpoints.

Configured hosts and ports are inputs to service startup.  Consumers use the
endpoint actually published by the running service, including the owning
instance identity, so a stale default or a healthy service from another HASHI
instance cannot silently satisfy a local route.
"""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.file_permissions import tighten_fd_permissions


SERVICE_ENDPOINT_SCHEMA_VERSION = 1
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::"})


class ServiceEndpointError(RuntimeError):
    """A service endpoint is absent, malformed, stale, or wrongly scoped."""


def normalize_instance_id(value: Any) -> str:
    normalized = str(value or "").strip().upper()
    if not normalized:
        raise ServiceEndpointError("instance identity is required")
    return normalized


def _is_wsl() -> bool:
    release = platform.uname().release.casefold()
    return "microsoft" in release or "wsl" in release


def _valid_host(value: Any) -> str | None:
    host = str(value or "").strip().strip("[]")
    if not host or any(character.isspace() for character in host):
        return None
    if any(character in host for character in ("/", "?", "#", "@")):
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not all(part and part.replace("-", "").isalnum() for part in host.split(".")):
            return None
    return host


def _add_host(values: list[str], value: Any) -> None:
    host = _valid_host(value)
    if host is None or host in _WILDCARD_HOSTS or host in values:
        return
    values.append(host)


def discover_local_hosts() -> tuple[str, ...]:
    """Discover connectable local addresses without embedding one machine IP."""

    loopback_aliases: list[str] = []
    interface_hosts: list[str] = []

    # WSL mirrored networking commonly provides a host-visible IPv4 alias on
    # the Linux loopback interface.  Its value is installation-specific, so it
    # must be discovered rather than named in source.
    if _is_wsl():
        try:
            completed = subprocess.run(
                ["ip", "-brief", "-4", "addr", "show", "lo"],
                capture_output=True,
                text=True,
                timeout=1.0,
                check=False,
            )
            for token in completed.stdout.split():
                if "/" in token:
                    _add_host(loopback_aliases, token.split("/", 1)[0])
        except (OSError, subprocess.SubprocessError):
            pass

    try:
        import ifaddr

        for adapter in ifaddr.get_adapters():
            for address in adapter.ips:
                if isinstance(address.ip, str):
                    _add_host(interface_hosts, address.ip)
    except Exception:
        pass

    try:
        for host in socket.gethostbyname_ex(socket.gethostname())[2]:
            _add_host(interface_hosts, host)
    except OSError:
        pass

    ordered: list[str] = []
    for host in (*loopback_aliases, *interface_hosts, "127.0.0.1"):
        _add_host(ordered, host)
    return tuple(ordered)


def discover_worker_callback_hosts(
    *,
    wsl: bool | None = None,
    route_output: str | None = None,
) -> tuple[str, ...]:
    """Return local hosts where a platform Worker can accept Core callbacks.

    With a native Core, loopback is the least-privileged route. A WSL Core
    needs the Windows host-side address of its current virtual network. That
    address is the live default gateway reported by the WSL routing table; it
    is installation- and boot-specific and must never be embedded in source.
    """

    if wsl is None:
        wsl = _is_wsl()
    discovered: list[str] = []
    if wsl:
        output = route_output
        if output is None:
            try:
                completed = subprocess.run(
                    ["ip", "-4", "route", "show", "default"],
                    capture_output=True,
                    text=True,
                    timeout=1.0,
                    check=False,
                )
                output = completed.stdout
            except (OSError, subprocess.SubprocessError):
                output = ""
        for line in str(output or "").splitlines():
            tokens = line.split()
            for index, token in enumerate(tokens[:-1]):
                if token == "via":
                    _add_host(discovered, tokens[index + 1])
                    break
    _add_host(discovered, "127.0.0.1")
    return tuple(discovered)


def host_can_bind(host: str) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind((host, 0))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def select_service_bind_host(
    configured_host: Any,
    *,
    candidates: Iterable[str] | None = None,
    can_bind: Callable[[str], bool] = host_can_bind,
    wsl: bool | None = None,
) -> str:
    """Select a bind address from configuration plus live interface discovery."""

    configured = _valid_host(configured_host) or "127.0.0.1"
    if configured not in _LOOPBACK_NAMES:
        return configured
    configured = "::1" if configured == "::1" else "127.0.0.1"
    if wsl is None:
        wsl = _is_wsl()
    if wsl:
        for candidate in candidates if candidates is not None else discover_local_hosts():
            host = _valid_host(candidate)
            if host and host not in _WILDCARD_HOSTS and can_bind(host):
                return host
    return configured


def connectable_host_for_bind(
    bind_host: Any,
    *,
    candidates: Iterable[str] | None = None,
) -> str:
    host = _valid_host(bind_host)
    if host is None:
        raise ServiceEndpointError("service bind host is malformed")
    if host not in _WILDCARD_HOSTS:
        return "127.0.0.1" if host == "localhost" else host
    for candidate in candidates if candidates is not None else discover_local_hosts():
        resolved = _valid_host(candidate)
        if resolved and resolved not in _WILDCARD_HOSTS:
            return resolved
    return "::1" if host == "::" else "127.0.0.1"


@dataclass(frozen=True)
class ServiceEndpoint:
    service: str
    instance_id: str
    scheme: str
    host: str
    port: int
    revision: int
    published_at: float
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        rendered_host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{rendered_host}:{self.port}"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["base_url"] = self.base_url
        return value


class ServiceEndpointRegistry:
    """Authoritative in-process registry for services owned by one Core."""

    def __init__(self, kernel: Any) -> None:
        self.kernel = kernel
        self._services: dict[str, ServiceEndpoint] = {}
        self._revision = 0

    @property
    def instance_id(self) -> str:
        configured = getattr(getattr(self.kernel, "global_cfg", None), "instance_id", None)
        path_identity = getattr(getattr(self.kernel, "paths", None), "instance_id", None)
        return normalize_instance_id(configured or path_identity or "HASHI")

    @property
    def state_path(self) -> Path:
        bridge_home = Path(getattr(self.kernel.paths, "bridge_home"))
        return bridge_home / "state" / "service_endpoints.json"

    def publish(
        self,
        service: str,
        *,
        instance_id: str,
        host: str,
        port: int,
        scheme: str = "http",
        metadata: Mapping[str, Any] | None = None,
    ) -> ServiceEndpoint:
        name = str(service or "").strip().casefold()
        if not name:
            raise ServiceEndpointError("service name is required")
        owner = normalize_instance_id(instance_id)
        if owner != self.instance_id:
            raise ServiceEndpointError(
                f"cross-instance endpoint publication rejected: expected={self.instance_id} received={owner}"
            )
        resolved_host = connectable_host_for_bind(host)
        resolved_port = int(port)
        if not 1 <= resolved_port <= 65535:
            raise ServiceEndpointError("service endpoint port must be between 1 and 65535")
        normalized_scheme = str(scheme or "").strip().casefold()
        if normalized_scheme not in {"http", "https"}:
            raise ServiceEndpointError("service endpoint scheme must be http or https")
        self._revision += 1
        endpoint = ServiceEndpoint(
            service=name,
            instance_id=owner,
            scheme=normalized_scheme,
            host=resolved_host,
            port=resolved_port,
            revision=self._revision,
            published_at=time.time(),
            metadata=dict(metadata or {}),
        )
        self._services[name] = endpoint
        self._persist()
        return endpoint

    def unpublish(self, service: str) -> None:
        name = str(service or "").strip().casefold()
        if self._services.pop(name, None) is not None:
            self._revision += 1
            self._persist()

    def resolve(
        self,
        service: str,
        *,
        expected_instance: str | None = None,
    ) -> ServiceEndpoint:
        name = str(service or "").strip().casefold()
        endpoint = self._services.get(name)
        if endpoint is None:
            raise ServiceEndpointError(f"live service endpoint is unavailable: {name}")
        expected = normalize_instance_id(expected_instance or self.instance_id)
        if endpoint.instance_id != expected or expected != self.instance_id:
            raise ServiceEndpointError(
                f"cross-instance service route rejected: expected={expected} published={endpoint.instance_id}"
            )
        return endpoint

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": SERVICE_ENDPOINT_SCHEMA_VERSION,
            "instance_id": self.instance_id,
            "revision": self._revision,
            "services": {
                name: endpoint.to_dict()
                for name, endpoint in sorted(self._services.items())
            },
        }

    def _persist(self) -> None:
        path = self.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            self.snapshot(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            tighten_fd_permissions(descriptor)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            Path(temporary).unlink(missing_ok=True)
            raise


def endpoint_from_snapshot(
    snapshot: Mapping[str, Any] | None,
    service: str,
    *,
    expected_instance: str,
) -> ServiceEndpoint:
    value = dict(snapshot or {})
    expected = normalize_instance_id(expected_instance)
    owner = normalize_instance_id(value.get("instance_id"))
    if owner != expected:
        raise ServiceEndpointError(
            f"cross-instance endpoint snapshot rejected: expected={expected} received={owner}"
        )
    raw = (value.get("services") or {}).get(str(service).strip().casefold())
    if not isinstance(raw, Mapping):
        raise ServiceEndpointError(f"live service endpoint is unavailable: {service}")
    endpoint_owner = normalize_instance_id(raw.get("instance_id"))
    if endpoint_owner != expected:
        raise ServiceEndpointError(
            f"cross-instance service route rejected: expected={expected} published={endpoint_owner}"
        )
    host = connectable_host_for_bind(raw.get("host"))
    port = int(raw.get("port") or 0)
    if not 1 <= port <= 65535:
        raise ServiceEndpointError("service endpoint port must be between 1 and 65535")
    return ServiceEndpoint(
        service=str(raw.get("service") or service).strip().casefold(),
        instance_id=endpoint_owner,
        scheme=str(raw.get("scheme") or "http").strip().casefold(),
        host=host,
        port=port,
        revision=int(raw.get("revision") or 0),
        published_at=float(raw.get("published_at") or 0),
        metadata=dict(raw.get("metadata") or {}),
    )


def load_service_endpoint(
    state_path: Path,
    service: str,
    *,
    expected_instance: str,
) -> ServiceEndpoint:
    """Load one persisted live receipt with the same identity checks as Core."""

    try:
        payload = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ServiceEndpointError(
            f"service endpoint state is unavailable: {state_path}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise ServiceEndpointError("service endpoint state must be a JSON object")
    if int(payload.get("schema_version") or 0) != SERVICE_ENDPOINT_SCHEMA_VERSION:
        raise ServiceEndpointError("service endpoint state schema is unsupported")
    return endpoint_from_snapshot(
        payload,
        service,
        expected_instance=expected_instance,
    )
