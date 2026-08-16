"""Agent-scoped client tools for the authoritative HASHI Scheduler API."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

from tools.workbench_client import request_workbench_json, workbench_endpoint

_request_json = request_workbench_json


def _scheduler_endpoint(audit_context: Mapping[str, Any] | None) -> tuple[str, str]:
    return workbench_endpoint(audit_context, require_agent=True)


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
            query = urlencode(
                {key: value for key, value in query_values.items() if value}
            )
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
