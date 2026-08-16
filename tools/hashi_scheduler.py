"""Agent-scoped client tools for the authoritative HASHI Scheduler API."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping
from urllib.parse import quote, urlencode, urlparse

import aiohttp


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _scheduler_endpoint(audit_context: Mapping[str, Any] | None) -> tuple[str, str]:
    context = audit_context or {}
    agent = str(context.get("agent_name") or "").strip()
    raw_base_url = str(context.get("scheduler_api_base_url") or "").strip().rstrip("/")
    if not agent:
        raise ValueError("HASHI Scheduler agent identity is unavailable")
    if not raw_base_url:
        raise ValueError("HASHI Scheduler API is unavailable in this gateway context")
    parsed = urlparse(raw_base_url)
    if parsed.scheme != "http" or str(parsed.hostname or "").casefold() not in _LOOPBACK_HOSTS:
        raise ValueError("HASHI Scheduler API must use an HTTP loopback endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("HASHI Scheduler API endpoint is malformed")
    return raw_base_url, agent


async def _request_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, url, json=payload) as response:
            try:
                body = await response.json()
            except (aiohttp.ContentTypeError, json.JSONDecodeError):
                text = (await response.text()).strip()
                body = {"ok": False, "error": text or "non-JSON scheduler response"}
            if not isinstance(body, dict):
                body = {"ok": False, "error": "invalid scheduler response shape"}
            return response.status, body


def _render_result(status: int, payload: dict[str, Any]) -> str:
    if status >= 400 or payload.get("ok") is False:
        detail = str(payload.get("error") or payload.get("message") or "request failed")
        return f"Error: HASHI Scheduler API request failed ({status}): {detail}"
    authoritative = {
        "authority": "HASHI Scheduler",
        "namespace": "hashi_scheduler",
        **payload,
    }
    return json.dumps(authoritative, ensure_ascii=False, indent=2, sort_keys=True)


async def execute_hashi_scheduler_tool(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    audit_context: Mapping[str, Any] | None,
) -> str:
    try:
        base_url, agent = _scheduler_endpoint(audit_context)
    except ValueError as exc:
        return f"Error: {exc}"

    encoded_agent = quote(agent, safe="")
    args = dict(arguments or {})
    try:
        if tool_name == "hashi_scheduler_list":
            query: dict[str, str] = {}
            kind = str(args.get("kind") or "all").strip().lower()
            if kind != "all":
                query["kind"] = kind
            if "enabled" in args:
                query["enabled"] = "true" if bool(args["enabled"]) else "false"
            suffix = f"?{urlencode(query)}" if query else ""
            status, payload = await _request_json(
                "GET",
                f"{base_url}/api/agents/{encoded_agent}/scheduler/jobs{suffix}",
            )
        elif tool_name == "hashi_scheduler_status":
            query = urlencode(
                {
                    "kind": str(args.get("kind") or "").strip().lower(),
                    "job_id": str(args.get("job_id") or "").strip(),
                }
            )
            status, payload = await _request_json(
                "GET",
                f"{base_url}/api/agents/{encoded_agent}/scheduler/status?{query}",
            )
        elif tool_name == "hashi_scheduler_run_history":
            query_values = {
                "kind": str(args.get("kind") or "all").strip().lower(),
                "job_id": str(args.get("job_id") or "").strip(),
                "limit": str(int(args.get("limit") or 10)),
            }
            query = urlencode({key: value for key, value in query_values.items() if value})
            status, payload = await _request_json(
                "GET",
                f"{base_url}/api/agents/{encoded_agent}/scheduler/runs?{query}",
            )
        elif tool_name == "hashi_scheduler_rerun":
            if args.get("authorization") != "explicit_user_authorization":
                return (
                    "Error: hashi_scheduler_rerun requires explicit authorization for "
                    "this exact single job"
                )
            status, payload = await _request_json(
                "POST",
                f"{base_url}/api/agents/{encoded_agent}/jobs/run",
                payload={
                    "kind": str(args.get("kind") or "").strip().lower(),
                    "job_id": str(args.get("job_id") or "").strip(),
                    "requested_by": "hashi_tool_gateway",
                    "authorization": "explicit_user_authorization",
                },
            )
        else:
            return f"Error: unsupported HASHI Scheduler tool '{tool_name}'"
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return f"Error: HASHI Scheduler API is unavailable: {type(exc).__name__}: {exc}"
    except (TypeError, ValueError) as exc:
        return f"Error: invalid HASHI Scheduler tool arguments: {exc}"

    return _render_result(status, payload)
