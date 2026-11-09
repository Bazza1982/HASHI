"""Private MCP transport back to the owning Function Worker.

Only the owner executes tools. Context-file roots, policy and secrets are
catalogue hints; they cannot expand the owner's authority.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import inspect
import logging
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from tools.registry import ToolResult


async def execute_via_owner(descriptor, name, arguments, call_id, *, on_tool_activity=None):
    parsed = urlsplit(str((descriptor or {}).get("url") or ""))
    token = str((descriptor or {}).get("token") or "")
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or not parsed.port or parsed.path != "/tool"
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not token):
        return ToolResult(tool_call_id=call_id, is_error=True,
            output="Error: runtime_protection_context_unavailable; invoke through the owning HASHI Run.",
            details={"control_disposition":"denied", "tool_executed":False,
                     "error_code":"runtime_protection_context_unavailable", "retryable":False})
    dispatch_id = uuid4().hex
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=None)

    def request():
        try:
            body = json.dumps({"tool_name":name, "arguments":arguments,
                               "tool_call_id":call_id, "dispatch_id":dispatch_id}).encode()
            connection.request("POST", "/tool", body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(48 * 1024 * 1024 + 1)
            if response.status != 200 or len(payload) > 48 * 1024 * 1024:
                raise RuntimeError("owner tool response unavailable")
            value = json.loads(payload)
            if not isinstance(value, dict) or type(value.get("is_error")) is not bool:
                raise RuntimeError("invalid owner tool response")
            return ToolResult(tool_call_id=call_id, output=str(value.get("output") or ""),
                is_error=value["is_error"], content=value.get("content"),
                details=value.get("details") or {})
        finally:
            connection.close()

    def cancel():
        # Cancellation is per call. It must not revoke concurrent calls in the Run.
        control = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        try:
            control.request("POST", "/tool-cancel",
                body=json.dumps({"dispatch_id":dispatch_id}).encode(), headers=headers)
            control.getresponse().read()
        finally:
            control.close()
            connection.close()

    def progress():
        control = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        try:
            control.request("POST", "/tool-status",
                body=json.dumps({"dispatch_id":dispatch_id}).encode(), headers=headers)
            response = control.getresponse()
            return json.loads(response.read(128 * 1024)) if response.status == 200 else {}
        finally:
            control.close()

    try:
        pending = asyncio.create_task(asyncio.to_thread(request))
        sequence = 0
        while callable(on_tool_activity) and not pending.done():
            done, _ = await asyncio.wait({pending}, timeout=1)
            if done:
                break
            try:
                facts = await asyncio.to_thread(progress)
                if int(facts.get("update_sequence") or 0) > sequence:
                    sequence = int(facts["update_sequence"])
                    value = on_tool_activity(SimpleNamespace(metadata=facts))
                    if inspect.isawaitable(value):
                        await value
            except Exception:
                pass  # Progress loss cannot replace the authoritative tool result.
        return await pending
    except asyncio.CancelledError:
        try:
            await asyncio.shield(asyncio.to_thread(cancel))
        except Exception:
            logging.getLogger(__name__).warning("Owner tool cancellation cleanup is unconfirmed")
        raise
    except Exception:
        # A transport loss after dispatch never proves that no effect occurred.
        return ToolResult(tool_call_id=call_id, is_error=True,
            output="Error: owner_tool_response_unconfirmed; inspect the request before retrying.",
            details={"control_disposition":"unknown",
                     "error_code":"owner_tool_response_unconfirmed", "retryable":False,
                     "effect_state":"unknown", "transport_state":"response_unconfirmed",
                     "recovery":"inspect_request_diagnostics_before_retry"})
# HASHI-Fingerprint: hfp_14a8a74cfb7b439da50c7834e9d61041
