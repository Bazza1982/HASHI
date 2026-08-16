"""Trusted client helpers for the local HASHI Workbench API."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import aiohttp


def workbench_endpoint(
    audit_context: Mapping[str, Any] | None,
    *,
    require_agent: bool = False,
) -> tuple[str, str]:
    """Resolve the serialized local Workbench endpoint and agent identity."""

    context = audit_context or {}
    agent = str(context.get("agent_name") or "").strip()
    raw_base_url = (
        str(
            context.get("workbench_api_base_url")
            or context.get("scheduler_api_base_url")
            or ""
        )
        .strip()
        .rstrip("/")
    )
    if require_agent and not agent:
        raise ValueError("HASHI agent identity is unavailable")
    if not raw_base_url:
        raise ValueError("HASHI Workbench API is unavailable in this gateway context")
    parsed = urlparse(raw_base_url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("HASHI Workbench API must use an HTTP endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("HASHI Workbench API endpoint is malformed")
    if parsed.path not in {"", "/"}:
        raise ValueError("HASHI Workbench API endpoint must not include a path")
    if str(parsed.hostname).casefold() in {"0.0.0.0", "::"}:
        raise ValueError("HASHI Workbench API endpoint must use a connectable host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("HASHI Workbench API endpoint has an invalid port") from exc
    if port is None:
        raise ValueError("HASHI Workbench API endpoint must include a port")
    return raw_base_url, agent


async def request_workbench_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = 30,
) -> tuple[int, dict[str, Any]]:
    """Perform one bounded request and normalize the Workbench JSON shape."""

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with (
        aiohttp.ClientSession(timeout=timeout) as session,
        session.request(method, url, json=payload) as response,
    ):
        try:
            body = await response.json()
        except (aiohttp.ContentTypeError, json.JSONDecodeError):
            text = (await response.text()).strip()
            body = {"ok": False, "error": text or "non-JSON Workbench response"}
        if not isinstance(body, dict):
            body = {"ok": False, "error": "invalid Workbench response shape"}
        return response.status, body
