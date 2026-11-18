"""Agent-scoped client tools for the authoritative HASHI Scheduler API."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

from tools.workbench_client import request_workbench_json, workbench_endpoint

_request_json = request_workbench_json


def _scheduler_endpoint(audit_context: Mapping[str, Any] | None) -> tuple[str, str]:
    return workbench_endpoint(audit_context, require_agent=True)


def governed_scheduler_context(audit_context: Mapping[str, Any] | None) -> bool:
    context = audit_context or {}
    config = context.get("global_config")
    profile = getattr(config, "deployment_profile", None)
    return (profile != "personal" if profile is not None
            else context.get("_scheduler_governed") is True)



def scheduler_tool_credential(secrets, audit_context, allowed_tools):
    """Keep the administrative key inside the trusted runtime, outside MCP context."""
    scoped = secrets.get("scheduler_tool_token")
    if scoped:
        return str(scoped)
    config = (audit_context or {}).get("global_config")
    governed = getattr(config, "deployment_profile", "personal") != "personal"
    owner = (audit_context or {}).get("owner_id") if governed else None
    if governed and (not isinstance(owner, str) or not owner.startswith("enterprise:")
                     or not owner.removeprefix("enterprise:").strip()):
        return ""
    secret = secrets.get("workbench_admin_token") if not governed else None
    if governed:
        root = getattr(config, "bridge_home", None)
        if root is not None:
            from orchestrator.workbench_auth import scheduler_delegation_key

            try:
                secret = scheduler_delegation_key(root)
            except (OSError, ValueError):
                return ""
    request = (audit_context or {}).get("request_id")
    instance = getattr(config, "instance_id", None) or (audit_context or {}).get("instance_id")
    agent = (audit_context or {}).get("agent_name")
    if not (secret and request and instance and agent):
        return ""
    from orchestrator.workbench_auth import issue_scheduler_tool_token

    base_url, _ = _scheduler_endpoint(audit_context)
    return issue_scheduler_tool_token(secret, instance_id=str(instance), agent_id=str(agent),
                                     request_id=str(request), audience=base_url,
                                     run_id=(audit_context or {}).get("hashi_run_id"),
                                     fencing_token=(audit_context or {}).get("hashi_fencing_token"),
                                     owner_id=owner,
                                     tools=[tool for tool in allowed_tools if tool.startswith(("hashi_scheduler_", "hashi_superloop_"))])

def _scheduler_credential_needs_renewal(credential: str) -> bool:
    """Inspect expiry only for scheduling; the API verifies every signed claim."""
    try:
        if len(credential) > 8192:
            return False
        body = credential.split(".", 1)[0]
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return type(claims.get("expires")) is int and claims["expires"] <= time.time() + 60
    except (ValueError, TypeError, AttributeError, UnicodeError):
        return False


async def renew_scheduler_tool_credential(credential, base_url, agent, tool_name, request_json):
    if not credential or not _scheduler_credential_needs_renewal(credential):
        return credential, None
    status, payload = await request_json(
        "POST", f"{base_url}/api/agents/{quote(agent, safe='')}/scheduler/token/renew",
        headers={"X-Hashi-Scheduler-Token": credential}, payload={"tool": tool_name},
    )
    if status >= 400 or payload.get("ok") is not True:
        return "", (status, payload)
    renewed = payload.get("token")
    if not isinstance(renewed, str) or not renewed or len(renewed) > 8192:
        return "", (503, {"ok": False, "error_code": "scheduler_delegation_unavailable"})
    return renewed, None


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
        if code == "approval_required":
            approval = payload.get("approval")
            approval_id = approval.get("approval_id") if isinstance(approval, dict) else None
            if isinstance(approval_id, str) and re.fullmatch(r"approval_[0-9a-f]{32}", approval_id):
                detail += f" Pending approval ID: {approval_id}"
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
    credential: str = "",
) -> str:
    if not credential and governed_scheduler_context(audit_context):
        return ("Error: scheduler_delegation_unavailable: The current Run has no usable Scheduler "
                "delegation. Check the live user binding and instance credential availability.")
    try:
        base_url, agent = _scheduler_endpoint(audit_context)
    except ValueError as exc:
        return f"Error: {exc}"

    async def request_json(method, url, **kwargs):
        if credential:
            kwargs["headers"] = {"X-Hashi-Scheduler-Token": credential}
        return await _request_json(method, url, **kwargs)

    encoded_agent = quote(agent, safe="")
    args = dict(arguments or {})
    try:
        credential, renewal_error = await renew_scheduler_tool_credential(
            credential, base_url, agent, tool_name, _request_json,
        )
        if renewal_error is not None:
            return _render_result(*renewal_error)
        if tool_name == "hashi_scheduler_list":
            query: dict[str, str] = {}
            kind = str(args.get("kind") or "all").strip().lower()
            if kind != "all":
                query["kind"] = kind
            if "enabled" in args:
                query["enabled"] = "true" if bool(args["enabled"]) else "false"
            suffix = f"?{urlencode(query)}" if query else ""
            status, payload = await request_json(
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
            status, payload = await request_json(
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
            status, payload = await request_json(
                "GET",
                f"{base_url}/api/agents/{encoded_agent}/scheduler/runs?{query}",
            )
        elif tool_name == "hashi_scheduler_rerun":
            if not str(args.get("job_id") or "").strip():
                return "Error: hashi_scheduler_rerun requires job_id"
            status, payload = await request_json(
                "POST",
                f"{base_url}/api/agents/{encoded_agent}/jobs/run",
                payload={
                    "kind": str(args.get("kind") or "").strip().lower(),
                    "job_id": str(args.get("job_id") or "").strip(),
                    "requested_by": "hashi_tool_gateway",
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
            status, payload = await request_json(
                "POST",
                f"{base_url}/api/agents/{encoded_agent}/scheduler/recovery/"
                f"{quote(batch_id, safe='')}/resolve",
                payload=payload,
            )
        elif tool_name == "hashi_scheduler_create":
            payload = dict(args)
            payload["requested_by"] = "hashi_tool_gateway"
            status, payload = await request_json(
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
            status, payload = await request_json(
                "PATCH",
                f"{base_url}/api/agents/{encoded_agent}/scheduler/jobs/{quote(job_id, safe='')}",
                payload=payload,
            )
        elif tool_name == "hashi_scheduler_delete":
            job_id = str(args.get("job_id") or "").strip()
            if not job_id:
                return "Error: hashi_scheduler_delete requires job_id"
            status, snapshot = await request_json(
                "GET",
                f"{base_url}/api/agents/{encoded_agent}/scheduler/status?"
                + urlencode({"kind": str(args.get("kind") or "").strip().lower(), "job_id": job_id}),
            )
            if status >= 400 or snapshot.get("ok") is False:
                return _render_result(status, snapshot)
            revision = (snapshot.get("status") or {}).get("deletion_revision")
            if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
                return "Error: HASHI Scheduler deletion requires an authenticated task snapshot"
            status, payload = await request_json(
                "DELETE",
                f"{base_url}/api/agents/{encoded_agent}/scheduler/jobs/{quote(job_id, safe='')}",
                payload={
                    "kind": str(args.get("kind") or "").strip().lower(),
                    "requested_by": "hashi_tool_gateway",
                    "expected_revision": revision,
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
# HASHI-Fingerprint: hfp_b8276aa6cac24043935744edb0bbdd9d
