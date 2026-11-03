"""Agent-scoped client tools for the authoritative HASHI Scheduler API."""

from __future__ import annotations

import asyncio
import json
import re
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
        raw_code = payload.get("error_code")
        code = (raw_code if isinstance(raw_code, str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}", raw_code) else "")
        details = []
        for key in ("error", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip() and value.strip() not in details:
                details.append(value.strip())
        reason = f" [{code}]" if code else ""
        detail = ": " + " ".join(details) if details else ""
        return f"Error: HASHI Scheduler API request failed ({status}){reason}{detail}"
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
        elif tool_name == "hashi_scheduler_recovery_resolve":
            batch_id = str(args.get("batch_id") or "").strip()
            if not batch_id:
                return "Error: hashi_scheduler_recovery_resolve requires batch_id"
            payload = {
                "action": str(args.get("action") or "").strip().lower(),
                "requested_by": "hashi_tool_gateway",
            }
            if args.get("counts") is not None:
                payload["counts"] = dict(args.get("counts") or {})
            status, payload = await _request_json(
                "POST",
                f"{base_url}/api/agents/{encoded_agent}/scheduler/recovery/"
                f"{quote(batch_id, safe='')}/resolve",
                payload=payload,
            )
        elif tool_name == "hashi_scheduler_create":
            payload = dict(args)
            payload["requested_by"] = "hashi_tool_gateway"
            status, payload = await _request_json(
                "POST",
                f"{base_url}/api/agents/{encoded_agent}/scheduler/jobs",
                payload=payload,
            )
        elif tool_name == "hashi_scheduler_update":
            job_id = str(args.get("job_id") or "").strip()
            if not job_id:
                return "Error: hashi_scheduler_update requires job_id"
            payload = dict(args)
            payload.pop("job_id", None)
            payload["requested_by"] = "hashi_tool_gateway"
            status, payload = await _request_json(
                "PATCH",
                f"{base_url}/api/agents/{encoded_agent}/scheduler/jobs/{quote(job_id, safe='')}",
                payload=payload,
            )
        elif tool_name == "hashi_scheduler_delete":
            if args.get("authorization") != "explicit_user_authorization":
                return (
                    "Error: hashi_scheduler_delete requires explicit authorization for "
                    "this exact task"
                )
            job_id = str(args.get("job_id") or "").strip()
            if not job_id:
                return "Error: hashi_scheduler_delete requires job_id"
            status, payload = await _request_json(
                "DELETE",
                f"{base_url}/api/agents/{encoded_agent}/scheduler/jobs/{quote(job_id, safe='')}",
                payload={
                    "kind": str(args.get("kind") or "").strip().lower(),
                    "requested_by": "hashi_tool_gateway",
                    "authorization": "explicit_user_authorization",
                },
            )
        else:
            return f"Error: unsupported HASHI Scheduler tool '{tool_name}'"
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        if isinstance(exc, asyncio.TimeoutError):
            code, detail = "scheduler_api_timeout", "The API response timed out."
        elif isinstance(exc, aiohttp.ServerDisconnectedError):
            code, detail = "scheduler_api_disconnected", "The connection closed before the response."
        elif isinstance(exc, aiohttp.ClientConnectionError):
            code, detail = "scheduler_api_connection_failed", "The API connection could not be established."
        else:
            code, detail = "scheduler_api_response_failed", "The API response could not be read."
        outcome = (
            "No task change was requested."
            if tool_name in {"hashi_scheduler_list", "hashi_scheduler_status", "hashi_scheduler_run_history"}
            else "The change result is unconfirmed; check task status before retrying."
        )
        return f"Error: HASHI Scheduler API [{code}]: {detail} {outcome}"
    except (TypeError, ValueError) as exc:
        return f"Error: invalid HASHI Scheduler tool arguments: {exc}"

    return _render_result(status, payload)
