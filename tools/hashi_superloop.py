"""Agent-scoped client tools for the authoritative Superloop API."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

from tools.workbench_client import request_workbench_json, workbench_endpoint

_request_json = request_workbench_json


def _render_result(status: int, payload: dict[str, Any]) -> str:
    if status >= 400 or payload.get("ok") is False:
        detail = str(payload.get("error") or payload.get("message") or "request failed")
        return f"Error: Superloop API request failed ({status}): {detail}"
    authoritative = {
        "authority": "HASHI Superloop",
        "namespace": "hashi_superloop",
        **payload,
    }
    return json.dumps(authoritative, ensure_ascii=False, indent=2, sort_keys=True)


async def execute_hashi_superloop_tool(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    audit_context: Mapping[str, Any] | None,
) -> str:
    try:
        base_url, agent = workbench_endpoint(audit_context, require_agent=True)
    except ValueError as exc:
        return f"Error: {exc}"

    encoded_agent = quote(agent, safe="")
    args = dict(arguments or {})
    try:
        if tool_name == "hashi_superloop_list":
            query = {}
            if bool(args.get("include_deleted", False)):
                query["include_deleted"] = "true"
            suffix = f"?{urlencode(query)}" if query else ""
            status, payload = await _request_json(
                "GET", f"{base_url}/api/agents/{encoded_agent}/superloops{suffix}"
            )
        elif tool_name == "hashi_superloop_get":
            loop_id = str(args.get("loop_id") or "").strip()
            if not loop_id:
                return "Error: hashi_superloop_get requires loop_id"
            status, payload = await _request_json(
                "GET",
                f"{base_url}/api/agents/{encoded_agent}/superloops/{quote(loop_id, safe='')}"
            )
        elif tool_name == "hashi_superloop_create":
            status, payload = await _request_json(
                "POST",
                f"{base_url}/api/agents/{encoded_agent}/superloops",
                payload={
                    "goal": str(args.get("goal") or "").strip(),
                    "task_title": str(args.get("task_title") or "").strip() or None,
                    "requested_by": "hashi_tool_gateway",
                },
            )
        elif tool_name == "hashi_superloop_update":
            loop_id = str(args.get("loop_id") or "").strip()
            if not loop_id:
                return "Error: hashi_superloop_update requires loop_id"
            payload = dict(args)
            payload.pop("loop_id", None)
            payload["requested_by"] = "hashi_tool_gateway"
            status, payload = await _request_json(
                "PATCH",
                f"{base_url}/api/agents/{encoded_agent}/superloops/{quote(loop_id, safe='')}",
                payload=payload,
            )
        elif tool_name == "hashi_superloop_delete":
            if args.get("authorization") != "explicit_user_authorization":
                return (
                    "Error: hashi_superloop_delete requires explicit authorization for "
                    "this exact loop"
                )
            loop_id = str(args.get("loop_id") or "").strip()
            if not loop_id:
                return "Error: hashi_superloop_delete requires loop_id"
            status, payload = await _request_json(
                "DELETE",
                f"{base_url}/api/agents/{encoded_agent}/superloops/{quote(loop_id, safe='')}",
                payload={
                    "requested_by": "hashi_tool_gateway",
                    "authorization": "explicit_user_authorization",
                },
            )
        else:
            return f"Error: unsupported HASHI Superloop tool '{tool_name}'"
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return f"Error: Superloop API is unavailable: {type(exc).__name__}: {exc}"
    except (TypeError, ValueError) as exc:
        return f"Error: invalid HASHI Superloop tool arguments: {exc}"

    return _render_result(status, payload)


__all__ = ["execute_hashi_superloop_tool"]
