from __future__ import annotations

import json
import os
import queue
import secrets
import threading
import time
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client
from pathlib import Path
from typing import Any


DEFAULT_WINDOWS_PIPE = os.environ.get(
    "HASHI_BROWSER_BRIDGE_PIPE",
    r"\\.\pipe\hashi-browser-bridge",
)
DEFAULT_WINDOWS_AUTH_FILE = Path(
    os.environ.get(
        "HASHI_BROWSER_BRIDGE_AUTH_FILE",
        str(
            Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
            / "HASHI"
            / "browser_bridge"
            / "bridge-auth.key"
        ),
    )
)


def is_windows_pipe(endpoint: str | Path) -> bool:
    return str(endpoint).lower().startswith("\\\\.\\pipe\\")


def load_auth_key(path: Path = DEFAULT_WINDOWS_AUTH_FILE, *, create: bool = False) -> bytes:
    """Load the per-user bridge key, optionally creating it without overwriting a peer."""

    if not path.exists():
        if not create:
            raise FileNotFoundError(f"browser bridge authentication file is missing: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(secrets.token_bytes(32))
            try:
                path.chmod(0o600)
            except OSError:
                pass
    key = path.read_bytes()
    if len(key) < 32:
        raise ValueError("browser bridge authentication key must contain at least 32 bytes")
    return key


def _wait_named_pipe(endpoint: str, timeout_ms: int) -> bool:
    if os.name != "nt":
        return False
    import ctypes

    return bool(ctypes.windll.kernel32.WaitNamedPipeW(endpoint, max(0, timeout_ms)))


def windows_pipe_available(endpoint: str, *, timeout_ms: int = 0) -> bool:
    return is_windows_pipe(endpoint) and _wait_named_pipe(endpoint, timeout_ms)


def _connect_windows_pipe(endpoint: str, auth_key: bytes, timeout_s: float):
    """Bound the multiprocessing authentication handshake, which has no timeout."""

    outcome: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
    cancelled = threading.Event()

    def connect() -> None:
        try:
            connection = Client(endpoint, family="AF_PIPE", authkey=auth_key)
            if cancelled.is_set():
                connection.close()
                return
            outcome.put(("ok", connection))
        except BaseException as exc:  # surfaced unchanged on the caller thread
            if not cancelled.is_set():
                outcome.put(("error", exc))

    thread = threading.Thread(
        target=connect,
        name="hashi-browser-pipe-connect",
        daemon=True,
    )
    thread.start()
    thread.join(max(0.05, timeout_s))
    if thread.is_alive():
        cancelled.set()
        raise TimeoutError("timed out authenticating to browser bridge named pipe")
    try:
        status, value = outcome.get_nowait()
    except queue.Empty as exc:
        raise OSError("browser bridge named-pipe connection ended without a result") from exc
    if status == "error":
        if isinstance(value, AuthenticationError):
            raise PermissionError("browser bridge named-pipe authentication failed") from value
        raise value
    return value


def send_windows_pipe_request(
    endpoint: str,
    request: dict[str, Any],
    *,
    auth_file: Path = DEFAULT_WINDOWS_AUTH_FILE,
    timeout_s: float = 20.0,
    connect_wait_s: float = 6.0,
) -> dict[str, Any]:
    if os.name != "nt":
        raise OSError("Windows named-pipe transport is unavailable on this platform")
    auth_key = load_auth_key(auth_file)
    deadline = time.monotonic() + max(0.0, connect_wait_s)
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        wait_ms = min(int(remaining * 1000), 350)
        if _wait_named_pipe(endpoint, wait_ms):
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(f"browser bridge named pipe is unavailable: {endpoint}")

    connection = _connect_windows_pipe(
        endpoint,
        auth_key,
        max(0.05, deadline - time.monotonic()),
    )
    try:
        # send_bytes avoids pickle deserialisation at the local trust boundary.
        connection.send_bytes(json.dumps(request, separators=(",", ":")).encode("utf-8"))
        if not connection.poll(timeout_s):
            raise TimeoutError("timed out waiting for browser bridge named-pipe response")
        payload = connection.recv_bytes()
    finally:
        connection.close()
    response = json.loads(payload.decode("utf-8"))
    if not isinstance(response, dict):
        raise ValueError("browser bridge response must be a JSON object")
    return response
