from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import logging
import re
import sys
import warnings
from collections import Counter
from pathlib import Path
from typing import Any, BinaryIO

from jsonschema import Draft7Validator

from tools.gateway.context import GatewayContext, load_gateway_context


PROTOCOL_VERSION = "2025-03-26"
SERVER_VERSION = "1.0"

_LEGACY_SCREENSHOT_TOOLS = {
    "browser_screenshot",
    "browser_session",
    "desktop_screenshot",
    "windows_screenshot",
}
_GATEWAY_EXPOSED_TOOL_NAMES = {
    "file_read": "hashi_file_read",
    "file_write": "hashi_file_write",
    "file_list": "hashi_file_list",
    "apply_patch": "hashi_apply_patch",
}
_GATEWAY_INTERNAL_TOOL_NAMES = {
    exposed: internal for internal, exposed in _GATEWAY_EXPOSED_TOOL_NAMES.items()
}


def exposed_tool_name(internal_name: str) -> str:
    return _GATEWAY_EXPOSED_TOOL_NAMES.get(internal_name, internal_name)
_LEGACY_SCREENSHOT_PATTERN = re.compile(
    r"(?P<data_url>data:(?P<mime>image/[a-z0-9.+-]+);base64,"
    r"(?P<data_payload>[A-Za-z0-9+/=]*))|"
    r"(?P<label>(?:^screenshot:|\[screenshot\]\s+base64:))"
    r"(?P<label_payload>[A-Za-z0-9+/=]*)",
    flags=re.IGNORECASE | re.MULTILINE,
)


def _normalize_legacy_screenshot(payload: str) -> tuple[dict[str, str], int]:
    from PIL import Image

    from tools.media_read import (
        IMAGE_MAX_BYTES,
        MediaReadError,
        _jpeg_bytes,
    )

    if len(payload) > ((IMAGE_MAX_BYTES + 2) // 3) * 4:
        raise MediaReadError("encoded screenshot exceeds the source safety limit")
    try:
        raw = base64.b64decode(payload, validate=True)
    except ValueError as exc:
        raise MediaReadError("screenshot returned malformed base64") from exc
    if not raw or len(raw) > IMAGE_MAX_BYTES:
        raise MediaReadError("screenshot is empty or exceeds the source safety limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                image.seek(0)
                encoded, _size = _jpeg_bytes(image.copy())
    except MediaReadError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise MediaReadError("screenshot dimensions exceed the decode safety limit") from exc
    except Exception as exc:
        raise MediaReadError(f"screenshot decode failed: {exc}") from exc
    return (
        {
            "type": "image",
            "mimeType": "image/jpeg",
            "data": base64.b64encode(encoded).decode("ascii"),
        },
        len(encoded),
    )


def _bridge_legacy_screenshot_output(
    tool_name: str,
    output: str,
) -> tuple[list[dict[str, Any]] | None, bool]:
    """Convert legacy screenshot strings into bounded MCP image blocks."""
    if tool_name not in _LEGACY_SCREENSHOT_TOOLS:
        return None, False
    matches = list(_LEGACY_SCREENSHOT_PATTERN.finditer(output))
    if not matches:
        return None, False
    from tools.media_read import MAX_TOTAL_IMAGE_BYTES

    content: list[dict[str, Any]] = []
    cursor = 0
    image_count = 0
    total_image_bytes = 0
    errors: list[str] = []
    for match in matches:
        text = output[cursor : match.start()].strip()
        if text:
            content.append({"type": "text", "text": text})
        payload = match.group("data_payload") or match.group("label_payload") or ""
        try:
            if image_count >= 6:
                raise ValueError("screenshot omitted after the 6-image safety limit")
            image, image_bytes = _normalize_legacy_screenshot(payload)
            if total_image_bytes + image_bytes > MAX_TOTAL_IMAGE_BYTES:
                raise ValueError("screenshot omitted at the total image-byte safety limit")
            content.append(image)
            image_count += 1
            total_image_bytes += image_bytes
        except ValueError as exc:
            errors.append(str(exc))
            content.append({"type": "text", "text": f"Screenshot unavailable: {exc}"})
        cursor = match.end()
    trailing = output[cursor:].strip()
    if trailing:
        content.append({"type": "text", "text": trailing})
    return content, image_count == 0 and bool(errors)


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

    async def call(self, name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
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
        result = await self.registry.execute(internal_name, arguments, tool_call_id=call_id)
        if not result.is_error and self._reports_state_change(result.output):
            self.fingerprints[fingerprint] = 0
        else:
            self.fingerprints[fingerprint] += 1
        self.consecutive_errors = self.consecutive_errors + 1 if result.is_error else 0
        content = result.content
        bridge_error = False
        if content is None:
            content, bridge_error = _bridge_legacy_screenshot_output(internal_name, result.output)
        return self._result(
            result.output,
            result.is_error or bridge_error,
            content,
        )

    def _result(
        self,
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
        transport = str(request.pop("_hashi_stdio_transport", "content_length"))
        try:
            response = await _dispatch(gateway, request)
        except Exception as exc:
            response = _error(request.get("id"), -32603, f"internal gateway error: {exc}")
        if response is not None:
            await asyncio.to_thread(
                _write_frame, stdout, response, transport=transport
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Expose HASHI ToolRegistry over MCP stdio")
    parser.add_argument("--context", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    asyncio.run(serve(args.context, sys.stdin.buffer, sys.stdout.buffer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
