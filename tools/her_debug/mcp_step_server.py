from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, BinaryIO

from .step_state import SequentialStepState, StepProtocolError


PROTOCOL_VERSION = "2025-03-26"


def _read_frame(stream: BinaryIO) -> dict[str, Any] | None:
    content_length: int | None = None
    while True:
        line = stream.readline()
        if not line:
            return None
        if line in {b"\r\n", b"\n"}:
            break
        name, separator, value = line.decode("ascii", errors="replace").partition(":")
        if separator and name.strip().casefold() == "content-length":
            content_length = int(value.strip())
    if content_length is None:
        raise ValueError("missing Content-Length header")
    payload = stream.read(content_length)
    if len(payload) != content_length:
        raise EOFError("incomplete MCP frame")
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("MCP request must be an object")
    return parsed


def _write_frame(stream: BinaryIO, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    stream.write(body)
    stream.flush()


def _tool_definition() -> dict[str, Any]:
    return {
        "name": "her_step",
        "description": "Accept exactly the next deterministic HER lab step token.",
        "inputSchema": {
            "type": "object",
            "properties": {"token": {"type": "string"}},
            "required": ["token"],
            "additionalProperties": False,
        },
    }


async def _dispatch(state: SequentialStepState, request: dict[str, Any]) -> dict[str, Any] | None:
    request_id = request.get("id")
    if request_id is None:
        return None
    method = request.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "her-step-lab", "version": "1.0"},
        }
    elif method == "tools/list":
        result = {"tools": [_tool_definition()]}
    elif method == "tools/call":
        params = request.get("params") or {}
        if params.get("name") != "her_step":
            result = {"content": [{"type": "text", "text": "unknown tool"}], "isError": True}
        else:
            arguments = params.get("arguments") or {}
            try:
                accepted = state.accept(str(arguments.get("token") or ""))
                result = {"content": [{"type": "text", "text": json.dumps(accepted, sort_keys=True)}], "isError": False}
            except StepProtocolError as exc:
                result = {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


async def serve(state: SequentialStepState, stdin: BinaryIO, stdout: BinaryIO) -> None:
    while True:
        request = await asyncio.to_thread(_read_frame, stdin)
        if request is None:
            return
        try:
            response = await _dispatch(state, request)
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32603, "message": f"internal step error: {exc}"}}
        if response is not None:
            await asyncio.to_thread(_write_frame, stdout, response)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HER sequential-step MCP test fixture")
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args(argv)
    asyncio.run(serve(SequentialStepState(args.state), sys.stdin.buffer, sys.stdout.buffer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
