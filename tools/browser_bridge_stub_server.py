from __future__ import annotations

import json
import socketserver
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _default_response(action: str, args: dict[str, Any]) -> dict[str, Any]:
    if action == "ping":
        return {"ok": True, "output": "pong"}
    if action == "get_text":
        return {"ok": True, "output": f"Stub text for {args.get('url', 'about:blank')}"}
    if action == "screenshot":
        return {"ok": True, "output": "stub-screenshot-bytes"}
    if action == "active_tab":
        return {"ok": True, "output": {"url": args.get("url", "about:blank")}}
    return {"ok": True, "output": f"stub:{action}"}


def _append_trace(trace_path: Path | None, event: dict[str, Any]) -> None:
    if trace_path is None:
        return
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    with trace_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


class _StubHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        request = json.loads(self.rfile.readline().decode("utf-8"))
        state = self.server.stub_state  # type: ignore[attr-defined]
        action = request["action"]
        args = request.get("args", {})
        _append_trace(
            state.get("trace_path"),
            {
                "event": "request",
                "action": action,
                "args": args,
            },
        )
        response = state["responses"].get(action) or _default_response(action, args)
        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
        self.wfile.flush()
        _append_trace(
            state.get("trace_path"),
            {
                "event": "response",
                "action": action,
                "ok": response.get("ok"),
            },
        )


class _StubServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@contextmanager
def running_stub_bridge(
    socket_path: Path,
    *,
    responses: dict[str, dict[str, Any]] | None = None,
    trace_path: Path | None = None,
) -> Iterator[Path]:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()

    server = _StubServer(str(socket_path), _StubHandler)
    server.stub_state = {"responses": responses or {}, "trace_path": trace_path}  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _append_trace(trace_path, {"event": "server_started", "socket_path": str(socket_path)})
    try:
        yield socket_path
    finally:
        server.shutdown()
        server.server_close()
        _append_trace(trace_path, {"event": "server_stopped", "socket_path": str(socket_path)})
        if socket_path.exists():
            socket_path.unlink()
