"""Trusted client helpers for the local HASHI Backend API."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import aiohttp

from orchestrator.service_endpoints import ServiceEndpointError


def _validate_backend_api_base_url(value: Any) -> str:
    raw_base_url = str(value or "").strip().rstrip("/")
    if not raw_base_url:
        raise ValueError("HASHI Backend API endpoint has no base URL")
    parsed = urlparse(raw_base_url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("HASHI Backend API must use an HTTP endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("HASHI Backend API endpoint is malformed")
    if parsed.path not in {"", "/"}:
        raise ValueError("HASHI Backend API endpoint must not include a path")
    if str(parsed.hostname).casefold() in {"0.0.0.0", "::"}:
        raise ValueError("HASHI Backend API endpoint must use a connectable host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("HASHI Backend API endpoint has an invalid port") from exc
    if port is None:
        raise ValueError("HASHI Backend API endpoint must include a port")
    return raw_base_url


def _expected_instance_id(
    context: Mapping[str, Any],
    global_config: Any = None,
) -> str:
    config = global_config or context.get("global_config")
    return str(
        getattr(config, "instance_id", None)
        or context.get("instance_id")
        or ""
    ).strip()


def _validate_live_endpoint(
    endpoint: Mapping[str, Any],
    *,
    expected_instance: str,
) -> str:
    service = str(endpoint.get("service") or "workbench").strip().casefold()
    if service != "workbench":
        raise ValueError(
            f"unexpected HASHI Backend API service endpoint: {service or 'missing'}"
        )
    published_instance = str(endpoint.get("instance_id") or "").strip()
    if expected_instance:
        if not published_instance:
            raise ValueError("live HASHI Backend API endpoint has no instance identity")
        if published_instance.casefold() != expected_instance.casefold():
            raise ValueError(
                "cross-instance HASHI Backend API endpoint rejected: "
                f"expected={expected_instance} received={published_instance}"
            )
    return _validate_backend_api_base_url(endpoint.get("base_url"))


def live_workbench_api_base_url(
    audit_context: Mapping[str, Any] | None,
    global_config: Any = None,
) -> str | None:
    """Resolve the running Backend API from authoritative runtime topology.

    HERV3 executes tools directly inside an Agent Function Worker. Its runtime
    facade carries the live, instance-scoped service topology rather than the
    serialized endpoint used by isolated CLI gateways.
    """

    context = audit_context or {}
    runtime = context.get("_runtime")
    kernel = (
        getattr(runtime, "orchestrator", None)
        or getattr(runtime, "kernel", None)
        or context.get("_kernel")
    )
    if kernel is None:
        return None
    expected_instance = _expected_instance_id(context, global_config)
    resolver = getattr(kernel, "resolve_service_endpoint", None)
    if callable(resolver):
        try:
            endpoint = resolver(
                "workbench",
                expected_instance=expected_instance or None,
            )
        except (ServiceEndpointError, TypeError, ValueError) as exc:
            raise ValueError(str(exc)) from exc
        if not isinstance(endpoint, Mapping):
            raise ValueError("live HASHI Backend API endpoint has an invalid shape")
        return _validate_live_endpoint(
            endpoint,
            expected_instance=expected_instance,
        )

    endpoint_registry = getattr(kernel, "endpoint_registry", None)
    if endpoint_registry is not None:
        try:
            endpoint = endpoint_registry.resolve(
                "workbench",
                expected_instance=expected_instance or None,
            )
        except (ServiceEndpointError, TypeError, ValueError) as exc:
            raise ValueError(str(exc)) from exc
        return _validate_live_endpoint(
            {
                "service": getattr(endpoint, "service", "workbench"),
                "instance_id": getattr(endpoint, "instance_id", ""),
                "base_url": getattr(endpoint, "base_url", ""),
            },
            expected_instance=expected_instance,
        )

    server = getattr(kernel, "workbench_api", None)
    if server is None:
        return None
    server_config = getattr(server, "global_config", None)
    server_instance = str(getattr(server_config, "instance_id", "") or "").strip()
    host = str(getattr(server, "bind_host", "") or "").strip()
    port = getattr(server, "bound_port", None)
    if not host or port is None:
        raise ValueError("live HASHI Backend API bind endpoint is unavailable")
    url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return _validate_live_endpoint(
        {
            "service": "workbench",
            "instance_id": server_instance,
            "base_url": f"http://{url_host}:{int(port)}",
        },
        expected_instance=expected_instance,
    )


def workbench_endpoint(
    audit_context: Mapping[str, Any] | None,
    *,
    require_agent: bool = False,
) -> tuple[str, str]:
    """Resolve the local Backend API endpoint and Agent identity."""

    context = audit_context or {}
    agent = str(context.get("agent_name") or "").strip()
    live_base_url = live_workbench_api_base_url(
        context,
        context.get("global_config"),
    )
    raw_base_url = live_base_url or str(
        context.get("workbench_api_base_url")
        or context.get("scheduler_api_base_url")
        or ""
    )
    if require_agent and not agent:
        raise ValueError("HASHI agent identity is unavailable")
    if not raw_base_url:
        raise ValueError("HASHI Backend API is unavailable in this tool context")
    return _validate_backend_api_base_url(raw_base_url), agent


async def request_workbench_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = 30,
) -> tuple[int, dict[str, Any]]:
    """Perform one bounded request and normalize the Backend API JSON shape."""

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with (
        aiohttp.ClientSession(timeout=timeout) as session,
        session.request(method, url, json=payload) as response,
    ):
        try:
            body = await response.json()
        except (aiohttp.ContentTypeError, json.JSONDecodeError):
            text = (await response.text()).strip()
            body = {"ok": False, "error": text or "non-JSON Backend API response"}
        if not isinstance(body, dict):
            body = {"ok": False, "error": "invalid Backend API response shape"}
        return response.status, body
