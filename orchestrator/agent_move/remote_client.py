"""Authenticated source-side transport for ``agent-move-v1``."""

from __future__ import annotations

import base64
import json
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

from orchestrator.runtime_defaults import DEFAULT_HASHI_REMOTE_PORT
from remote.security.client_auth import build_client_auth_headers
from remote.security.shared_token import HEADER_NONCE, verify_response_auth

from .package import (
    AGENT_MOVE_CAPABILITY,
    RETAINED_IDENTITY_CAPABILITY,
    AgentMoveArchive,
    AgentMoveError,
    package_sha256,
    read_agent_move_package,
)
from .transport_crypto import ENVELOPE_SCHEME, encrypt_package_transport


class AgentMoveRemoteError(AgentMoveError):
    """A target rejected or could not receive an Agent move operation."""


@dataclass(frozen=True)
class AgentMoveRemoteClient:
    base_url: str
    target_instance: str
    source_instance: str
    shared_token: str
    capabilities: dict[str, Any]

    def stage(self, package_path: Path | str, *, timeout: int = 300) -> dict[str, Any]:
        path = Path(package_path)
        package = read_agent_move_package(path, verify=True)
        self.ensure_package_compatible(package)
        package_bytes = path.stat().st_size
        try:
            receiver_limit = int(self.capabilities.get("max_package_bytes") or 0)
        except (TypeError, ValueError):
            receiver_limit = 0
        if receiver_limit > 0 and package_bytes > receiver_limit:
            raise AgentMoveRemoteError(
                f"Agent move package is {package_bytes} bytes; "
                f"receiver limit is {receiver_limit} bytes"
            )
        content = path.read_bytes()
        digest = package_sha256(path)
        envelope = encrypt_package_transport(
            content,
            shared_token=self.shared_token,
            source_instance=self.source_instance,
            target_instance=self.target_instance,
            package_sha256=digest,
        )
        return self._request(
            "/agent-move/v1/stage",
            method="POST",
            payload={
                "from_instance": self.source_instance,
                "encryption": ENVELOPE_SCHEME,
                "package_b64": base64.b64encode(envelope).decode("ascii"),
                "sha256": digest,
            },
            timeout=timeout,
        )

    def ensure_package_compatible(self, package: AgentMoveArchive) -> None:
        """Reject unsupported schemas before any package bytes leave the source."""

        try:
            schema = int(package.manifest.get("schema_version") or 0)
            schema_min = int(self.capabilities.get("schema_min") or 0)
            schema_max = int(self.capabilities.get("schema_max") or 0)
        except (TypeError, ValueError) as exc:
            raise AgentMoveRemoteError(
                "target receiver reported invalid Agent move schema limits"
            ) from exc
        if not schema_min <= schema <= schema_max:
            retained = (
                " and retained AGENT.md support"
                if package.retained_identity is not None
                else ""
            )
            raise AgentMoveRemoteError(
                f"package requires schema {schema}{retained}, but "
                f"{self.target_instance} receiver accepts schema "
                f"{schema_min} through {schema_max}; update/reboot the target receiver first"
            )
        advertised = {
            str(item)
            for item in (self.capabilities.get("capabilities") or [])
            if str(item)
        }
        primary = str(self.capabilities.get("capability") or "")
        if primary:
            advertised.add(primary)
        required = {
            str(item)
            for item in package.manifest.get("required_receiver_capabilities", [])
            if str(item)
        }
        missing = sorted(required - advertised)
        if missing:
            detail = (
                "retained AGENT.md support"
                if RETAINED_IDENTITY_CAPABILITY in missing
                else ", ".join(missing)
            )
            raise AgentMoveRemoteError(
                f"{self.target_instance} receiver does not advertise {detail}; "
                "update/reboot the target receiver first"
            )

    def commit(self, package_id: str, *, timeout: int = 300) -> dict[str, Any]:
        return self._action("commit", package_id, timeout=timeout)

    def activate(self, package_id: str, *, timeout: int = 60) -> dict[str, Any]:
        return self._action("activate", package_id, timeout=timeout)

    def rollback(self, package_id: str, *, timeout: int = 120) -> dict[str, Any]:
        return self._action("rollback", package_id, timeout=timeout)

    def status(self, package_id: str, *, timeout: int = 30) -> dict[str, Any]:
        return self._request(
            f"/agent-move/v1/status/{package_id}",
            method="GET",
            timeout=timeout,
        )

    def _action(self, action: str, package_id: str, *, timeout: int) -> dict[str, Any]:
        return self._request(
            f"/agent-move/v1/{action}",
            method="POST",
            payload={"from_instance": self.source_instance, "package_id": package_id},
            timeout=timeout,
        )

    def _request(
        self,
        path: str,
        *,
        method: str,
        payload: dict[str, Any] | None = None,
        timeout: int,
    ) -> dict[str, Any]:
        return _request_json(
            f"{self.base_url}{path}",
            method=method,
            payload=payload,
            shared_token=self.shared_token,
            from_instance=self.source_instance,
            timeout=timeout,
        )


def connect_agent_move_receiver(
    instances: Mapping[str, Any],
    target_instance: str,
    *,
    source_instance: str,
    shared_token: str | None,
    timeout: int = 5,
) -> AgentMoveRemoteClient:
    """Resolve and verify a live receiver; never trust a path from instances.json."""

    target = _normalize_instance(target_instance)
    source = _normalize_instance(source_instance)
    if target == source:
        raise AgentMoveRemoteError(
            "source and target HASHI instances must be different"
        )
    if not shared_token:
        raise AgentMoveRemoteError(
            "HASHI Remote shared-token pairing is required for Agent moves"
        )
    entry = _instance_entry(instances, target)
    port = _remote_port(entry)
    errors: list[str] = []
    for base_url in _candidate_base_urls(entry, port):
        try:
            health = _request_json(f"{base_url}/health", method="GET", timeout=timeout)
            live_id = _health_instance_id(health)
            if live_id != target:
                errors.append(f"{base_url}: answered as {live_id or 'unknown'}")
                continue
            capabilities = _request_json(
                f"{base_url}/agent-move/v1/capabilities",
                method="GET",
                shared_token=shared_token,
                from_instance=source,
                timeout=timeout,
            )
            if capabilities.get("capability") != AGENT_MOVE_CAPABILITY:
                errors.append(f"{base_url}: receiver capability is missing")
                continue
            receiver_id = _normalize_instance(capabilities.get("instance_id"))
            if receiver_id != target:
                errors.append(
                    f"{base_url}: authenticated receiver identified itself as "
                    f"{receiver_id or 'unknown'}"
                )
                continue
            authenticated_source = _normalize_instance(
                capabilities.get("authenticated_instance")
            )
            if authenticated_source != source:
                errors.append(
                    f"{base_url}: receiver authenticated the source as "
                    f"{authenticated_source or 'unknown'}"
                )
                continue
            if capabilities.get("authenticated_response_proof") is not True:
                errors.append(
                    f"{base_url}: authenticated response proof is not advertised"
                )
                continue
            encryption = capabilities.get("package_encryption") or []
            if ENVELOPE_SCHEME not in encryption:
                errors.append(
                    f"{base_url}: encrypted Agent move transport is not supported"
                )
                continue
            try:
                schema_min = int(capabilities.get("schema_min", 0))
                schema_max = int(capabilities.get("schema_max", 0))
                package_limit = int(capabilities.get("max_package_bytes", 0))
            except (TypeError, ValueError):
                errors.append(f"{base_url}: receiver limits are invalid")
                continue
            if schema_min > 1 or schema_max < 1:
                errors.append(f"{base_url}: agent-move-v1 schema is not accepted")
                continue
            if package_limit <= 0:
                errors.append(f"{base_url}: receiver package limit is invalid")
                continue
            return AgentMoveRemoteClient(
                base_url=base_url,
                target_instance=target,
                source_instance=source,
                shared_token=shared_token,
                capabilities=capabilities,
            )
        except (AgentMoveRemoteError, OSError, TimeoutError, URLError) as exc:
            errors.append(f"{base_url}: {exc}")
    detail = "; ".join(errors[-4:]) or "no usable Remote address"
    raise AgentMoveRemoteError(
        f"{target} does not expose {AGENT_MOVE_CAPABILITY}; update/reboot the target receiver first ({detail})"
    )


def _request_json(
    url: str,
    *,
    method: str,
    payload: dict[str, Any] | None = None,
    shared_token: str | None = None,
    from_instance: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    try:
        headers = build_client_auth_headers(
            url=url,
            method=method,
            data=data or b"",
            token=None,
            shared_token=shared_token,
            from_instance=from_instance,
            normalize_instance=lambda value: (
                _normalize_instance(value) if value else None
            ),
        )
    except ValueError as exc:
        raise AgentMoveRemoteError(str(exc)) from exc
    request = urllib_request.Request(url, data=data, headers=headers, method=method)
    request_nonce = str(headers.get(HEADER_NONCE) or "")
    kwargs: dict[str, Any] = {"timeout": timeout}
    if url.lower().startswith("https://"):
        kwargs["context"] = ssl.create_default_context()
    try:
        with urllib_request.urlopen(request, **kwargs) as response:
            raw = response.read()
    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
            detail = (
                body.get("error") or body.get("detail") or str(body)
                if isinstance(body, dict)
                else str(body)
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = str(exc)
        raise AgentMoveRemoteError(
            f"receiver returned HTTP {exc.code}: {detail}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise AgentMoveRemoteError(f"receiver request failed: {exc}") from exc
    try:
        result = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise AgentMoveRemoteError(
            "receiver returned an invalid JSON response"
        ) from exc
    if not isinstance(result, dict):
        raise AgentMoveRemoteError("receiver response must be a JSON object")
    if shared_token:
        response_auth = result.pop("response_auth", None)
        if not request_nonce or not verify_response_auth(
            shared_token=shared_token,
            request_nonce=request_nonce,
            payload=result,
            response_auth=response_auth,
        ):
            raise AgentMoveRemoteError(
                "receiver response authentication failed; refusing Agent move state changes"
            )
    if result.get("ok") is False:
        raise AgentMoveRemoteError(str(result.get("error") or result))
    return result


def request_authenticated_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    shared_token: str | None,
    from_instance: str,
    timeout: int = 30,
) -> dict[str, Any]:
    """Public read/write transport primitive with request and response HMAC."""

    return _request_json(
        url,
        method=method,
        payload=payload,
        shared_token=shared_token,
        from_instance=from_instance,
        timeout=timeout,
    )


def candidate_remote_base_urls(entry: Mapping[str, Any]) -> list[str]:
    """Resolve bounded Remote URLs using the same policy as Agent move."""

    return _candidate_base_urls(entry, _remote_port(entry))


def _instance_entry(instances: Mapping[str, Any], target: str) -> dict[str, Any]:
    for key, raw in instances.items():
        if _normalize_instance(key) == target or (
            isinstance(raw, dict)
            and _normalize_instance(raw.get("instance_id")) == target
        ):
            if not isinstance(raw, dict):
                break
            return dict(raw)
    known = ", ".join(sorted(str(key) for key in instances)) or "none"
    raise AgentMoveRemoteError(
        f"unknown target instance {target}; known instances: {known}"
    )


def _remote_port(entry: Mapping[str, Any]) -> int:
    try:
        value = int(
            entry.get("remote_port") or entry.get("port") or DEFAULT_HASHI_REMOTE_PORT
        )
    except Exception as exc:
        raise AgentMoveRemoteError("target Remote port is invalid") from exc
    if value < 1 or value > 65535:
        raise AgentMoveRemoteError("target Remote port is invalid")
    return value


def _candidate_base_urls(entry: Mapping[str, Any], port: int) -> list[str]:
    hosts: list[str] = []
    for item in entry.get("address_candidates", []) or []:
        if isinstance(item, dict):
            _add_unique(hosts, item.get("host"))
    for key in (
        "host",
        "lan_ip",
        "tailscale_ip",
        "internet_host",
        "api_host",
        "same_host_loopback",
    ):
        _add_unique(hosts, entry.get(key))
    if not hosts:
        hosts.append("127.0.0.1")
    configured_scheme = str(entry.get("scheme") or "").strip().lower()
    use_tls = bool(entry.get("use_tls")) or configured_scheme == "https"
    schemes = ["https"] if use_tls else ["http"]
    if configured_scheme == "http" and not use_tls:
        schemes = ["http"]
    if bool(entry.get("allow_http_fallback")) and "http" not in schemes:
        schemes.append("http")
    return [
        f"{scheme}://{_url_host(host)}:{port}" for host in hosts for scheme in schemes
    ]


def _url_host(host: str) -> str:
    value = str(host).strip()
    if ":" in value and not value.startswith("["):
        return f"[{value}]"
    return value


def _add_unique(values: list[str], raw: Any) -> None:
    value = str(raw or "").strip()
    if value and value not in {"0.0.0.0", "::"} and value not in values:
        values.append(value)


def _health_instance_id(health: Mapping[str, Any]) -> str:
    raw = health.get("instance")
    if isinstance(raw, dict):
        return (
            _normalize_instance(raw.get("instance_id"))
            if raw.get("instance_id")
            else ""
        )
    return _normalize_instance(raw) if raw else ""


def _normalize_instance(value: Any) -> str:
    return str(value or "").strip().upper()
