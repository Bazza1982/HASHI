from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any, BinaryIO

# Installed MCP children fast-admit and bind before importing product tooling.
# The parent passes its pinned admission reference, never an Agent-selected root.
if (__name__ == "__main__" and (Path(sys.prefix) / "runtime-policy").is_dir()
        and ("--runtime-manifest" not in sys.argv or "--owner-proxy-required" not in sys.argv)):
    raise SystemExit("runtime_protection_context_unavailable: installed MCP requires its owner admission")
if __name__ == "__main__" and "--runtime-manifest" in sys.argv:
    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument("--runtime-manifest", type=Path, required=True)
    bootstrap.add_argument("--runtime-manifest-digest", required=True)
    binding, _ = bootstrap.parse_known_args()
    from orchestrator.runtime_contract import (
        FUNCTIONS_ROLE, admitted_runtime_from_reference, bind_process_runtime,
        enforce_runtime_contract,
    )
    root = Path(__file__).resolve().parents[2]
    qualified = admitted_runtime_from_reference(
        {"path":str(binding.runtime_manifest), "digest":binding.runtime_manifest_digest},
        code_root=root, role=FUNCTIONS_ROLE)
    bind_process_runtime(qualified, code_root=root)
    enforce_runtime_contract(root, role=FUNCTIONS_ROLE)

from jsonschema import Draft7Validator

from tools.gateway.context import GatewayContext, load_gateway_context
from tools.legacy_screenshot import (
    continuation_note, legacy_screenshot_page,
)


PROTOCOL_VERSION = "2025-03-26"
SERVER_VERSION = "1.0"

_GATEWAY_EXPOSED_TOOL_NAMES = {
    "file_read": "hashi_file_read",
    "file_write": "hashi_file_write",
    "file_list": "hashi_file_list",
    "apply_patch": "hashi_apply_patch",
}
_GATEWAY_INTERNAL_TOOL_NAMES = {
    exposed: internal for internal, exposed in _GATEWAY_EXPOSED_TOOL_NAMES.items()
}

_SAFE_MCP_RESULT_KEYS = frozenset({
    "control_disposition",
    "tool_executed",
    "error_code",
    "operation",
    "reason",
    "retryable",
    "actual_bytes",
    "limit_bytes",
    "recovery",
    "effect_state",
    "transport_state",
    "cleanup_confirmed",
    "runtime_protection",
    "output_truncated",
    "complete_content",
    "output_original_chars",
    "output_continuation",
    "output_retention",
    "content_text_truncated",
    "content_continuations",
    "file_read_continuation",
    "image_continuation",
    "image_next_offset",
    "image_total",
    "image_page_count_budget",
    "image_page_byte_budget",
})
_SAFE_RUNTIME_PROTECTION_KEYS = frozenset({
    "qualification",
    "error_code",
    "protection_complete",
    "new_process_admission",
    "execution_role",
    "qualification_scope",
    "instance_id",
    "agent_id",
    "request_id",
    "session_id",
    "run_id",
    "recovery",
})


def _safe_mcp_result_details(details: Any) -> dict[str, Any]:
    """Project only model-safe control facts from the authoritative owner."""

    if not isinstance(details, dict):
        return {}
    safe: dict[str, Any] = {}
    for key in _SAFE_MCP_RESULT_KEYS:
        if key not in details:
            continue
        value = details[key]
        if key in {"actual_bytes", "limit_bytes"}:
            if type(value) is int and value >= 0:
                safe[key] = value
        elif key == "runtime_protection":
            if isinstance(value, dict):
                nested = {
                    nested_key: nested_value[:512] if isinstance(nested_value, str) else nested_value
                    for nested_key, nested_value in value.items()
                    if nested_key in _SAFE_RUNTIME_PROTECTION_KEYS
                    and (nested_value is None or isinstance(nested_value, (str, int, float, bool)))
                }
                if nested:
                    safe[key] = nested
        elif key == "content_continuations" and isinstance(value, list):
            safe[key] = [
                {"index": item["index"], "reference": item["reference"][:128]}
                for item in value
                if isinstance(item, dict) and type(item.get("index")) is int
                and isinstance(item.get("reference"), str)
            ]
        elif value is None or isinstance(value, (str, int, float, bool)):
            safe[key] = value[:512] if isinstance(value, str) else value
    return safe


def exposed_tool_name(internal_name: str) -> str:
    return _GATEWAY_EXPOSED_TOOL_NAMES.get(internal_name, internal_name)
def _bridge_legacy_screenshot_output(
    tool_name: str, output: str, continuation_reference: str | None = None,
    *, frame_budget: int | None = None, byte_budget: int | None = None,
) -> tuple[list[dict[str, Any]] | None, bool]:
    """Project the first bounded image page; the owner retains any continuation."""
    page = legacy_screenshot_page(
        tool_name, output, frame_budget=frame_budget, byte_budget=byte_budget,
    )
    if page is None:
        return None, False
    content = page["content"]
    if page["next_offset"] is not None:
        content.append({"type": "text", "text": continuation_note(
            continuation_reference, page["next_offset"], page["total"])})
    return content, page["is_error"]


class ToolGateway:
    def __init__(self, context: GatewayContext):
        self.context = context
        self.registry = None if context.audit.get("tool_gateway_required") else context.build_registry()
        self.call_count = 0
        self.consecutive_errors = 0
        self.fingerprints: Counter[str] = Counter()

    @staticmethod
    def _reports_state_change(output: str) -> bool:
        try:
            payload = json.loads(output)
        except (TypeError, json.JSONDecodeError):
            return False

        def walk(value: Any) -> bool:
            if isinstance(value, dict):
                if value.get("state_changed") is True:
                    return True
                return any(walk(item) for item in value.values())
            if isinstance(value, list):
                return any(walk(item) for item in value)
            return False

        return walk(payload)

    def tool_definitions(self) -> list[dict[str, Any]]:
        definitions = []
        catalog = (self.context.owner_tool_definitions if self.registry is None
                   else self.registry.get_tool_definitions())
        for definition in catalog:
            function = definition["function"]
            internal_name = function["name"]
            exposed_name = exposed_tool_name(internal_name)
            description = function.get("description", "")
            if internal_name in _GATEWAY_EXPOSED_TOOL_NAMES:
                description = (
                    f"HASHI filesystem authority (access_root scoped). {description}"
                )
            definitions.append(
                {
                    "name": exposed_name,
                    "description": description,
                    "inputSchema": function.get("parameters") or {"type": "object"},
                }
            )
        return definitions

    async def call(self, name: str, arguments: dict[str, Any], call_id: str,
                   *, on_tool_activity=None) -> dict[str, Any]:
        self.call_count += 1
        if self.context.enforce_legacy_limits and self.call_count > self.context.max_calls:
            return self._result(
                f"Error: legacy HER v1 tool gateway stopped after {self.context.max_calls} calls; report partial progress.",
                True,
            )

        schema = next(
            (
                item["inputSchema"]
                for item in self.tool_definitions()
                if item["name"] == name
            ),
            None,
        )
        if schema is None:
            return self._result(f"Error: unknown or unavailable HASHI tool '{name}'", True)
        errors = sorted(Draft7Validator(schema).iter_errors(arguments), key=lambda item: list(item.path))
        if errors:
            details = "; ".join(error.message for error in errors[:3])
            return self._result(f"Error: invalid arguments for '{name}': {details}", True)

        fingerprint = hashlib.sha256(
            json.dumps([name, arguments], ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if (
            self.context.enforce_legacy_limits
            and self.fingerprints[fingerprint] >= self.context.max_identical_calls
        ):
            return self._result(
                f"Error: legacy HER v1 gateway repeated identical call to '{name}' stopped after "
                f"{self.context.max_identical_calls} attempts; inspect state and report partial progress.",
                True,
            )
        if (
            self.context.enforce_legacy_limits
            and self.consecutive_errors >= self.context.max_consecutive_errors
        ):
            return self._result(
                f"Error: legacy HER v1 tool circuit breaker opened after {self.context.max_consecutive_errors} consecutive failures; "
                "stop retrying and report the failures.",
                True,
            )

        internal_name = _GATEWAY_INTERNAL_TOOL_NAMES.get(name, name)
        if self.context.audit.get("tool_gateway_required"):
            from tools.gateway.owner_proxy import execute_via_owner
            result = await execute_via_owner(
                self.context.audit.get("tool_gateway_proxy"), internal_name, arguments, call_id,
                on_tool_activity=on_tool_activity)
        elif callable(on_tool_activity):
            result = await self.registry.execute_with_audit_context(
                internal_name, arguments, tool_call_id=call_id,
                audit_context={"tool_activity_observer": on_tool_activity})
        else:
            result = await self.registry.execute(internal_name, arguments, tool_call_id=call_id)
        if not result.is_error and self._reports_state_change(result.output):
            self.fingerprints[fingerprint] = 0
        else:
            self.fingerprints[fingerprint] += 1
        self.consecutive_errors = self.consecutive_errors + 1 if result.is_error else 0
        content = result.content
        bridge_error = False
        if content is None:
            owner_details = result.details or {}
            content, bridge_error = _bridge_legacy_screenshot_output(
                internal_name, result.output, owner_details.get("image_continuation"),
                frame_budget=owner_details.get("image_page_count_budget"),
                byte_budget=owner_details.get("image_page_byte_budget"),
            )
        return self._result(
            result.output,
            result.is_error or bridge_error,
            content,
            result.details,
        )

    def _result(
        self,
        output: str,
        is_error: bool,
        content: list[dict[str, Any]] | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        safe_content: list[dict[str, Any]] = []
        image_filtered = False
        for block in content or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                safe_content.append({"type": "text", "text": block["text"]})
            elif (
                kind == "image"
                and self.context.vision_enabled
                and block.get("mimeType") in {"image/jpeg", "image/png", "image/gif", "image/webp"}
                and isinstance(block.get("data"), str)
            ):
                safe_content.append(
                    {
                        "type": "image",
                        "mimeType": block["mimeType"],
                        "data": block["data"],
                    }
                )
            elif kind == "image" and not self.context.vision_enabled:
                image_filtered = True
        if image_filtered and not safe_content:
            safe_content.append({"type": "text", "text": "Image content is unavailable in this non-vision context."})
        if not safe_content:
            safe_content = [{"type": "text", "text": output}]
        safe_details = _safe_mcp_result_details(details)
        if is_error and safe_details:
            safe_content.append({
                "type": "text",
                "text": "HASHI result details: " + json.dumps(
                    safe_details, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            })
        result = {
            "content": safe_content,
            "isError": is_error,
        }
        if safe_details:
            result["structuredContent"] = {
                "hashi": {"isError": is_error, "details": safe_details}
            }
            result["_meta"] = {"hashi/toolResultDetails": safe_details}
        return result


def _read_frame(stream: BinaryIO) -> dict[str, Any] | None:
    content_length = None
    saw_header = False
    while True:
        line = stream.readline()
        if not line:
            return None if not saw_header else None
        saw_header = True
        if line.lstrip().startswith(b"{"):
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("MCP request must be a JSON object")
            value["_hashi_stdio_transport"] = "jsonl"
            return value
        if line in {b"\r\n", b"\n"}:
            break
        name, separator, value = line.decode("ascii", errors="replace").partition(":")
        if separator and name.strip().casefold() == "content-length":
            content_length = int(value.strip())
    if content_length is None:
        raise ValueError("missing Content-Length header")
    payload = stream.read(content_length)
    if len(payload) != content_length:
        raise EOFError("MCP frame ended before Content-Length bytes were read")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("MCP request must be a JSON object")
    return value


def _write_frame(
    stream: BinaryIO,
    payload: dict[str, Any],
    *,
    transport: str = "content_length",
) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if transport == "jsonl":
        stream.write(body + b"\n")
        stream.flush()
        return
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    stream.write(body)
    stream.flush()


async def _dispatch(gateway: ToolGateway, request: dict[str, Any]) -> dict[str, Any] | None:
    request_id = request.get("id")
    if request_id is None:
        return None
    method = request.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "hashi-tools", "version": SERVER_VERSION},
        }
    elif method == "tools/list":
        result = {"tools": gateway.tool_definitions()}
    elif method == "tools/call":
        params = request.get("params") or {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error(request_id, -32602, "tools/call arguments must be an object")
        result = await gateway.call(name, arguments, str(request_id))
    else:
        return _error(request_id, -32601, f"method not found: {method}")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


async def serve(context_path: Path, stdin: BinaryIO, stdout: BinaryIO, *,
                owner_required: bool = False, context_digest: str | None = None) -> None:
    context = load_gateway_context(context_path, expected_digest=context_digest)
    if owner_required:
        from dataclasses import replace
        context = replace(context, audit={**context.audit, "tool_gateway_required": True})
    gateway = ToolGateway(context)
    pending: dict[str, asyncio.Task] = {}
    write_lock = asyncio.Lock()

    async def send(response, transport):
        async with write_lock:
            await asyncio.to_thread(_write_frame, stdout, response, transport=transport)

    async def handle(request, transport):
        request_id = request.get("id")
        try:
            if request.get("method") == "tools/call":
                params = request.get("params") or {}
                token = (params.get("_meta") or {}).get("progressToken")
                async def progress(event):
                    if token is None or not (event.metadata or {}).get("visible_update"):
                        return
                    from orchestrator.request_activity import _tool_activity_fields
                    facts = _tool_activity_fields(event.metadata)
                    if facts is None:
                        return
                    await send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {
                        "progressToken": token, "progress": facts["elapsed_ms"] / 1000,
                        "message": facts["display_summary"], "_meta": {"tool_activity": facts}}}, transport)
                arguments = params.get("arguments") or {}
                if not isinstance(arguments, dict):
                    response = _error(request_id, -32602, "tools/call arguments must be an object")
                else:
                    result = await gateway.call(str(params.get("name") or ""), arguments,
                                                str(request_id), on_tool_activity=progress)
                    response = {"jsonrpc": "2.0", "id": request_id, "result": result}
            else:
                response = await _dispatch(gateway, request)
        except asyncio.CancelledError:
            response = _error(request_id, -32800, "request cancelled; check the owner cleanup receipt before retrying")
        except Exception as exc:
            response = _error(request_id, -32603, f"internal gateway error: {exc}")
        if response is not None:
            await send(response, transport)

    try:
        while True:
            request = await asyncio.to_thread(_read_frame, stdin)
            if request is None:
                break
            transport = str(request.pop("_hashi_stdio_transport", "content_length"))
            if request.get("method") == "notifications/cancelled":
                target = pending.get(str((request.get("params") or {}).get("requestId")))
                if target:
                    target.cancel()
                continue
            key = str(request.get("id"))
            if key in pending or len(pending) >= 8:
                await send(_error(request.get("id"), -32600, "duplicate request or gateway concurrency limit"), transport)
                continue
            task = asyncio.create_task(handle(request, transport))
            pending[key] = task
            task.add_done_callback(lambda done, key=key: pending.pop(key, None))
        if pending:
            await asyncio.gather(*tuple(pending.values()))
    finally:
        for task in tuple(pending.values()):
            task.cancel()
        if pending:
            await asyncio.gather(*tuple(pending.values()), return_exceptions=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Expose HASHI ToolRegistry over MCP stdio")
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--owner-proxy-required", action="store_true")
    parser.add_argument("--context-digest")
    parser.add_argument("--runtime-manifest", type=Path)
    parser.add_argument("--runtime-manifest-digest")
    args = parser.parse_args()
    if args.owner_proxy_required and not args.context_digest:
        parser.error("--owner-proxy-required needs its admitted context digest")
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    asyncio.run(serve(args.context, sys.stdin.buffer, sys.stdout.buffer,
        owner_required=args.owner_proxy_required, context_digest=args.context_digest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_a46c42d66f764fbbb6aa65768a2a59c7
