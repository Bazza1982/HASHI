from __future__ import annotations

import json
import socketserver
import threading
from contextlib import contextmanager
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


class _StubHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        request = json.loads(self.rfile.readline().decode("utf-8"))
        state = self.server.stub_state  # type: ignore[attr-defined]
        action = request["action"]
        args = request.get("args", {})
        response = state["responses"].get(action) or _default_response(action, args)
        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
        self.wfile.flush()


class _StubServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@contextmanager
def running_stub_bridge(
    socket_path: Path,
    *,
    responses: dict[str, dict[str, Any]] | None = None,
) -> Iterator[Path]:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()

    server = _StubServer(str(socket_path), _StubHandler)
    server.stub_state = {"responses": responses or {}}  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield socket_path
    finally:
        server.shutdown()
        server.server_close()
        if socket_path.exists():
            socket_path.unlink()
