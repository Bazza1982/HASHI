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

from jsonschema import Draft7Validator

from tools.gateway.context import GatewayContext, load_gateway_context


PROTOCOL_VERSION = "2025-03-26"
SERVER_VERSION = "1.0"


class ToolGateway:
    def __init__(self, context: GatewayContext):
        self.context = context
        self.registry = context.build_registry()
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
        for definition in self.registry.get_tool_definitions():
            function = definition["function"]
            definitions.append(
                {
                    "name": function["name"],
                    "description": function.get("description", ""),
                    "inputSchema": function.get("parameters") or {"type": "object"},
                }
            )
        return definitions

    async def call(self, name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
        self.call_count += 1
        if self.call_count > self.context.max_calls:
            return self._result(
                f"Error: HASHI tool gateway stopped after {self.context.max_calls} calls; report partial progress.",
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
        if self.fingerprints[fingerprint] >= self.context.max_identical_calls:
            return self._result(
                f"Error: repeated identical call to '{name}' stopped after "
                f"{self.context.max_identical_calls} attempts; inspect state and report partial progress.",
                True,
            )
        if self.consecutive_errors >= self.context.max_consecutive_errors:
            return self._result(
                f"Error: tool circuit breaker opened after {self.context.max_consecutive_errors} consecutive failures; "
                "stop retrying and report the failures.",
                True,
            )

        result = await self.registry.execute(name, arguments, tool_call_id=call_id)
        if not result.is_error and self._reports_state_change(result.output):
            self.fingerprints[fingerprint] = 0
        else:
            self.fingerprints[fingerprint] += 1
        self.consecutive_errors = self.consecutive_errors + 1 if result.is_error else 0
        return self._result(result.output, result.is_error, result.content)

    @staticmethod
    def _result(
        output: str,
        is_error: bool,
        content: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        safe_content: list[dict[str, Any]] = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                safe_content.append({"type": "text", "text": block["text"]})
            elif (
                kind == "image"
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
        if not safe_content:
            safe_content = [{"type": "text", "text": output}]
        return {
            "content": safe_content,
            "isError": is_error,
        }


def _read_frame(stream: BinaryIO) -> dict[str, Any] | None:
    content_length = None
    saw_header = False
    while True:
        line = stream.readline()
        if not line:
            return None if not saw_header else None
        saw_header = True
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


def _write_frame(stream: BinaryIO, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
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


async def serve(context_path: Path, stdin: BinaryIO, stdout: BinaryIO) -> None:
    gateway = ToolGateway(load_gateway_context(context_path))
    while True:
        try:
            request = await asyncio.to_thread(_read_frame, stdin)
        except Exception as exc:
            logging.error("MCP frame error: %s", exc)
            return
        if request is None:
            return
        try:
            response = await _dispatch(gateway, request)
        except Exception as exc:
            response = _error(request.get("id"), -32603, f"internal gateway error: {exc}")
        if response is not None:
            await asyncio.to_thread(_write_frame, stdout, response)


def main() -> int:
    parser = argparse.ArgumentParser(description="Expose HASHI ToolRegistry over MCP stdio")
    parser.add_argument("--context", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    asyncio.run(serve(args.context, sys.stdin.buffer, sys.stdout.buffer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
