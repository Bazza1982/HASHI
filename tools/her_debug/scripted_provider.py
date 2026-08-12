from __future__ import annotations

import json
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .step_state import SequentialStepState


EXACT_REASONING_FRAGMENTS = (" leading", "  repeated", "\n", "\t", "", "trailing ")
EXACT_FINAL_FRAGMENTS = ("HER", "_", "SCRIPTED", "_OK")


@dataclass
class ScriptedProvider:
    scenario: str = "exact_stream"
    step_state: SequentialStepState | None = None
    delay_seconds: float = 0.0
    requests: list[dict[str, Any]] = field(default_factory=list)
    expected_disconnects: int = 0
    _server: ThreadingHTTPServer | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)

    def __enter__(self) -> "ScriptedProvider":
        parent = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    body = {"_malformed_request": True}
                parent.requests.append({"path": self.path, "body": body})
                try:
                    parent._respond(self, body, len(parent.requests))
                except (BrokenPipeError, ConnectionResetError):
                    parent.expected_disconnects += 1

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="her-scripted-provider", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("scripted provider is not running")
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def _respond(self, handler: BaseHTTPRequestHandler, body: dict[str, Any], request_number: int) -> None:
        if self.delay_seconds and (self.scenario != "delayed_response_once" or request_number == 1):
            time.sleep(self.delay_seconds)
        if self.scenario == "connection_reset_once" and request_number == 1:
            handler.connection.shutdown(socket.SHUT_RDWR)
            handler.connection.close()
            return
        status_match = re.fullmatch(r"http_(\d+)(?:_once)?", self.scenario)
        one_shot_status = self.scenario.endswith("_once")
        if status_match and not (one_shot_status and request_number > 1):
            status = int(status_match.group(1))
            payload = json.dumps({"error": {"message": f"scripted status {status}", "type": "scripted"}}).encode()
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            if status == 429:
                handler.send_header("Retry-After", "0")
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(payload)
            return

        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")
        handler.end_headers()
        if self.scenario == "malformed_sse":
            handler.wfile.write(b"data: {not-json}\n\n")
            handler.wfile.flush()
            return
        if self.scenario == "truncated_sse":
            handler.wfile.write(b'data: {"id":"truncated"')
            handler.wfile.flush()
            return
        chunks = self._chunks(body, request_number)
        for chunk in chunks:
            handler.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()

    def _chunks(self, body: dict[str, Any], request_number: int) -> list[dict[str, Any]]:
        if self.scenario == "thinking_then_final" and request_number == 1:
            return self._reasoning_only("tool-free visible finalization required")
        if self.scenario == "repeated_thinking_only":
            return self._reasoning_only(f"thinking-only-{request_number}")
        if self.scenario == "sequential_steps":
            return self._step_chunks(body)
        if self.scenario == "exact_stream":
            chunks: list[dict[str, Any]] = []
            for fragment in EXACT_REASONING_FRAGMENTS:
                chunks.append(self._delta(reasoning_content=fragment))
            for fragment in EXACT_FINAL_FRAGMENTS:
                chunks.append(self._delta(content=fragment))
            chunks.append(self._finish("stop"))
            return chunks
        if self.scenario == "thinking_then_final":
            return [self._delta(content="VISIBLE_FINAL_OK"), self._finish("stop")]
        return [self._delta(content="SCRIPTED_OK"), self._finish("stop")]

    def _step_chunks(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        if self.step_state is None:
            raise RuntimeError("sequential_steps requires step_state")
        payload = self.step_state.load()
        if int(payload["accepted_steps"]) >= int(payload["target_steps"]):
            return [self._delta(content="SEQUENTIAL_STEPS_COMPLETE"), self._finish("stop")]
        token = self.step_state.expected_token()
        tool_name = "mcp__her-step-lab__her_step"
        available = {
            str(item.get("function", {}).get("name"))
            for item in body.get("tools", [])
            if isinstance(item, dict)
        }
        if tool_name not in available:
            return [self._delta(content="SEQUENTIAL_STEP_TOOL_MISSING"), self._finish("stop")]
        call = {
            "index": 0,
            "id": f"step-call-{int(payload['accepted_steps']) + 1}",
            "type": "function",
            "function": {"name": tool_name, "arguments": json.dumps({"token": token})},
        }
        return [self._delta(tool_calls=[call]), self._finish("tool_calls")]

    @staticmethod
    def _delta(**delta: Any) -> dict[str, Any]:
        return {"id": "chatcmpl-her-debug", "choices": [{"delta": delta}]}

    @staticmethod
    def _finish(reason: str) -> dict[str, Any]:
        return {"id": "chatcmpl-her-debug", "choices": [{"delta": {}, "finish_reason": reason}]}

    @classmethod
    def _reasoning_only(cls, text: str) -> list[dict[str, Any]]:
        return [cls._delta(reasoning_content=text), cls._finish("stop")]

    def sanitized_requests(self) -> list[dict[str, Any]]:
        sanitized = []
        for request in self.requests:
            body = dict(request.get("body") or {})
            # These are synthetic requests, but retain only protocol-shape metadata by default.
            sanitized.append(
                {
                    "path": request.get("path"),
                    "model": body.get("model"),
                    "stream": body.get("stream"),
                    "message_roles": [item.get("role") for item in body.get("messages", []) if isinstance(item, dict)],
                    "tool_names": [
                        item.get("function", {}).get("name")
                        for item in body.get("tools", [])
                        if isinstance(item, dict)
                    ],
                }
            )
        return sanitized
