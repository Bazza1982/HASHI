"""Provider-neutral authenticated local desktop-browser adapter (Functions).

Only a configured descriptor is read. No global filesystem discovery, browser
extension fallback, per-action shell, or proprietary desktop implementation.
"""
from __future__ import annotations

import json
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Any

MAX_DESCRIPTOR_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_REQUEST_BYTES = 2 * 1024 * 1024
# The live-tab contract deliberately excludes arbitrary page scripts, raw HTML,
# upload and browser-wide password/extension management.
SUPPORTED_ACTIONS = frozenset({
    "active_tab", "get_text", "screenshot", "get_attribute", "wait_for",
    "fill", "type_text", "select", "key", "scroll", "hover", "click",
})


class EmbeddedBrowserError(RuntimeError):
    pass


class EmbeddedBrowserBridge:
    def __init__(self, descriptor: str | Path, instance_id: str, *, timeout: float = 30.0):
        self.path = Path(descriptor).expanduser().resolve()
        self.instance_id = str(instance_id).strip().upper()
        self.timeout = max(0.1, min(float(timeout), 120.0))
        if not self.instance_id:
            raise EmbeddedBrowserError("instance identity is required")

    def descriptor(self) -> dict[str, Any]:
        with self.path.open("rb") as stream:
            raw = stream.read(MAX_DESCRIPTOR_BYTES + 1)
        if len(raw) > MAX_DESCRIPTOR_BYTES:
            raise EmbeddedBrowserError("browser descriptor is too large")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise EmbeddedBrowserError("unsupported browser descriptor")
        if value.get("enabled") is not True:
            raise EmbeddedBrowserError("desktop browser is not accepting Agent actions")
        # This binding is written by the desktop integration, not by the model.
        if str(value.get("hashi_instance_id") or "").upper() != self.instance_id:
            raise EmbeddedBrowserError("desktop browser belongs to another HASHI instance")
        if value.get("transport") not in {"windows_named_pipe_jsonl", "unix_jsonl"}:
            raise EmbeddedBrowserError("unsupported desktop browser transport")
        endpoint = str(value.get("endpoint") or "")
        if value["transport"] == "windows_named_pipe_jsonl":
            if os.name != "nt" or not endpoint.startswith("\\\\.\\pipe\\"):
                raise EmbeddedBrowserError("a native Windows Worker is required for this named pipe")
        elif not Path(endpoint).is_absolute() or "://" in endpoint:
            raise EmbeddedBrowserError("browser endpoint must be a local Unix socket")
        key = Path(str(value.get("auth_file") or "")).expanduser().resolve()
        # Key material is only accepted beside the configured descriptor. It is
        # never taken from a model argument or sent to a network destination.
        if key.parent != self.path.parent or key.name != "agent-bridge.key":
            raise EmbeddedBrowserError("browser authentication file is outside its descriptor directory")
        if not value.get("instance_id"):
            raise EmbeddedBrowserError("browser generation is missing")
        return value

    def call(self, action: str, args: dict | None = None) -> Any:
        descriptor = self.descriptor()
        key = Path(descriptor["auth_file"]).read_text(encoding="utf-8").strip()
        if len(key) != 64 or any(c not in "0123456789abcdefABCDEF" for c in key):
            raise EmbeddedBrowserError("browser authentication key is invalid")
        scoped_args = dict(args or {})
        scoped_args["_browser_instance_id"] = descriptor["instance_id"]
        request = {
            "request_id": str(uuid.uuid4()), "auth_token": key,
            "action": str(action), "args": scoped_args,
            "browser_instance_id": descriptor["instance_id"],
        }
        encoded = (json.dumps(request, ensure_ascii=False, allow_nan=False) + "\n").encode()
        if len(encoded) > MAX_REQUEST_BYTES:
            raise EmbeddedBrowserError("browser request is too large")
        if descriptor["transport"] == "windows_named_pipe_jsonl":
            from tools.embedded_browser_pipe import exchange
            response = json.loads(exchange(descriptor["endpoint"], encoded,
                                           timeout=self.timeout, max_response=MAX_RESPONSE_BYTES))
        else:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                deadline = time.monotonic() + self.timeout
                client.settimeout(self.timeout)
                client.connect(descriptor["endpoint"])
                client.sendall(encoded)
                chunks = bytearray()
                while b"\n" not in chunks:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise EmbeddedBrowserError("browser response timed out")
                    client.settimeout(remaining)
                    block = client.recv(min(65536, MAX_RESPONSE_BYTES + 1 - len(chunks)))
                    if not block:
                        break
                    chunks.extend(block)
                    if len(chunks) > MAX_RESPONSE_BYTES:
                        raise EmbeddedBrowserError("browser response is too large")
            response = json.loads(bytes(chunks).split(b"\n", 1)[0])
        if not isinstance(response, dict):
            raise EmbeddedBrowserError("browser response must be an object")
        if response.get("ok") is not True:
            raise EmbeddedBrowserError(str((response or {}).get("error_code") or (response or {}).get("error") or "browser action failed"))
        if response.get("request_id") not in {None, "", request["request_id"]}:
            raise EmbeddedBrowserError("browser response correlation mismatch")
        result = response.get("result", response.get("output", response))
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict) and parsed.get("ok") is False:
                    raise EmbeddedBrowserError(str(parsed.get("error_code") or parsed.get("error") or "browser action failed"))
            except json.JSONDecodeError:
                pass
        return result

    def health(self) -> dict:
        try:
            result = self.call("workbench_health", {"hashi_instance_id": self.instance_id})
            if isinstance(result, str):
                result = json.loads(result)
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise EmbeddedBrowserError("desktop browser is unavailable")
            if str(result.get("hashi_instance_id") or "").upper() != self.instance_id:
                raise EmbeddedBrowserError("desktop browser health identity mismatch")
            return {"connected": True, "provider": "embedded", "generation": result.get("generation")}
        except (OSError, ValueError, EmbeddedBrowserError) as exc:
            return {"connected": False, "provider": "embedded", "error": str(exc)}

    def execute(self, action: str, arguments: dict) -> str:
        if action not in SUPPORTED_ACTIONS:
            raise EmbeddedBrowserError(f"unsupported embedded browser action: {action}")
        args = dict(arguments)
        args.pop("_authorized_roots", None)
        args.pop("browser_target", None)
        audit = dict(args.get("_audit") or {})
        audit["instance_id"] = self.instance_id
        args["_audit"] = audit
        if (
            action in {"get_text", "screenshot"}
            and str(args.get("handoff_id") or "").strip()
            and str(args.get("tab_id") or "").strip()
            and str(args.get("url") or "").strip()
        ):
            # Ordinary URL-bearing actions navigate before reading. A live-tab
            # read must preserve the existing document and its in-page state.
            # Check the requested URL against that exact authenticated handoff,
            # then read it without sending the navigation instruction.
            requested_url = str(args.pop("url")).strip()
            current = self.call("active_tab", {**args, "maximize": False})
            if isinstance(current, str):
                try:
                    current = json.loads(current)
                except json.JSONDecodeError:
                    current = None
            if not isinstance(current, dict) or current.get("url") != requested_url:
                raise EmbeddedBrowserError("browser_handoff_url_mismatch")
        result = self.call(action, args)
        if action == "screenshot" and isinstance(result, str) and result.startswith("data:image/"):
            return "screenshot:" + result.partition(",")[2]
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
