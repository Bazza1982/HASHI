"""Versioned, JSON-only IPC used between HASHI Core and Function Workers.

The multiprocessing connection is only a private byte transport.  No Python
object is pickled across the boundary: every frame is validated JSON carrying
an explicit protocol version.  This keeps Agent workers independent from the
Core's in-memory classes and makes compatibility failures deterministic.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

FUNCTION_WORKER_PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024 * 1024
DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0
READER_POLL_INTERVAL_SECONDS = 0.1

REQUEST = "request"
RESPONSE = "response"
EVENT = "event"
_MESSAGE_KINDS = frozenset({REQUEST, RESPONSE, EVENT})


class FunctionWorkerProtocolError(RuntimeError):
    """A worker IPC frame or state transition violated the Core contract."""


class FunctionWorkerDisconnected(FunctionWorkerProtocolError):
    """The peer process exited or its private channel became unavailable."""


class FunctionWorkerRemoteError(FunctionWorkerProtocolError):
    """The peer rejected a valid request."""

    def __init__(self, method: str, error: Mapping[str, Any]) -> None:
        self.method = method
        self.error = dict(error)
        message = str(error.get("message") or "remote request failed")
        error_type = str(error.get("type") or "RemoteError")
        super().__init__(f"{method}: {error_type}: {message}")


def json_value(value: Any, *, path: str = "$") -> Any:
    """Return a strictly JSON-compatible copy or raise with field provenance."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise FunctionWorkerProtocolError(f"non-finite number at {path}")
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise FunctionWorkerProtocolError(
                    f"non-string object key at {path}: {type(key).__name__}"
                )
            result[key] = json_value(item, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [json_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    raise FunctionWorkerProtocolError(
        f"unsupported IPC value at {path}: {type(value).__name__}"
    )


def _require_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FunctionWorkerProtocolError(f"{field} must be a non-empty string")
    return value


def validate_envelope(value: Any) -> dict[str, Any]:
    """Validate one decoded envelope and return a defensive JSON copy."""

    if not isinstance(value, Mapping):
        raise FunctionWorkerProtocolError("IPC envelope must be an object")
    message = json_value(value)
    version = message.get("version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != FUNCTION_WORKER_PROTOCOL_VERSION
    ):
        raise FunctionWorkerProtocolError(
            "Function Worker protocol mismatch: "
            f"received={message.get('version')!r} "
            f"required={FUNCTION_WORKER_PROTOCOL_VERSION}"
        )
    kind = message.get("kind")
    if kind not in _MESSAGE_KINDS:
        raise FunctionWorkerProtocolError(f"unknown IPC message kind: {kind!r}")
    allowed_fields = {
        REQUEST: {"version", "kind", "id", "method", "params"},
        RESPONSE: {"version", "kind", "id", "ok", "result", "error"},
        EVENT: {"version", "kind", "event", "payload"},
    }[kind]
    unexpected = sorted(set(message) - allowed_fields)
    if unexpected:
        raise FunctionWorkerProtocolError(
            f"unexpected {kind} envelope fields: {unexpected}"
        )
    if kind == REQUEST:
        _require_text(message.get("id"), field="request.id")
        _require_text(message.get("method"), field="request.method")
        if "params" not in message or not isinstance(message["params"], Mapping):
            raise FunctionWorkerProtocolError("request.params must be an object")
    elif kind == RESPONSE:
        _require_text(message.get("id"), field="response.id")
        if not isinstance(message.get("ok"), bool):
            raise FunctionWorkerProtocolError("response.ok must be a boolean")
        if message["ok"] and "result" not in message:
            raise FunctionWorkerProtocolError("successful response requires result")
        if not message["ok"] and not isinstance(message.get("error"), Mapping):
            raise FunctionWorkerProtocolError("failed response requires error object")
        if message["ok"] and "error" in message:
            raise FunctionWorkerProtocolError("successful response cannot include error")
        if not message["ok"] and "result" in message:
            raise FunctionWorkerProtocolError("failed response cannot include result")
    else:
        _require_text(message.get("event"), field="event.event")
        if "payload" not in message or not isinstance(message["payload"], Mapping):
            raise FunctionWorkerProtocolError("event.payload must be an object")
    return message


def encode_envelope(value: Mapping[str, Any]) -> bytes:
    message = validate_envelope(value)
    try:
        payload = json.dumps(
            message,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FunctionWorkerProtocolError(f"IPC envelope is not valid JSON: {exc}") from exc
    if len(payload) > MAX_FRAME_BYTES:
        raise FunctionWorkerProtocolError(
            f"IPC frame exceeds {MAX_FRAME_BYTES} bytes: {len(payload)}"
        )
    return payload


def decode_envelope(payload: bytes) -> dict[str, Any]:
    if len(payload) > MAX_FRAME_BYTES:
        raise FunctionWorkerProtocolError(
            f"IPC frame exceeds {MAX_FRAME_BYTES} bytes: {len(payload)}"
        )
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FunctionWorkerProtocolError("IPC frame is not valid UTF-8 JSON") from exc
    return validate_envelope(decoded)


RequestHandler = Callable[[str, dict[str, Any]], Awaitable[Any] | Any]
EventHandler = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class JsonConnectionPeer:
    """Concurrent bidirectional request/event peer over a private byte pipe."""

    def __init__(
        self,
        connection: Any,
        *,
        label: str,
        request_handler: RequestHandler | None = None,
        event_handler: EventHandler | None = None,
    ) -> None:
        self.connection = connection
        self.label = label
        self.request_handler = request_handler
        self.event_handler = event_handler
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, tuple[str, asyncio.Future[Any]]] = {}
        self._dispatch_tasks: set[asyncio.Task[Any]] = set()
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        self._closing = False
        self._disconnect_error: BaseException | None = None

    @property
    def is_closed(self) -> bool:
        return self._closed.is_set()

    def start(self) -> None:
        if self._reader_task is not None:
            return
        self._reader_task = asyncio.create_task(
            self._reader_loop(),
            name=f"worker-ipc-reader:{self.label}",
        )

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> Any:
        if self._closing or self.is_closed:
            raise FunctionWorkerDisconnected(f"{self.label} is disconnected")
        if self._reader_task is None:
            self.start()
        request_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (method, future)
        try:
            await self._send(
                {
                    "version": FUNCTION_WORKER_PROTOCOL_VERSION,
                    "kind": REQUEST,
                    "id": request_id,
                    "method": str(method),
                    "params": dict(params or {}),
                }
            )
            return await asyncio.wait_for(future, timeout=max(0.01, float(timeout)))
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"{self.label} request {method!r} exceeded {float(timeout):.1f}s"
            ) from exc
        finally:
            self._pending.pop(request_id, None)

    async def emit(
        self,
        event: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        if self._closing or self.is_closed:
            raise FunctionWorkerDisconnected(f"{self.label} is disconnected")
        await self._send(
            {
                "version": FUNCTION_WORKER_PROTOCOL_VERSION,
                "kind": EVENT,
                "event": str(event),
                "payload": dict(payload or {}),
            }
        )

    async def _send(self, envelope: Mapping[str, Any]) -> None:
        payload = encode_envelope(envelope)
        async with self._send_lock:
            try:
                await asyncio.to_thread(self.connection.send_bytes, payload)
            except (BrokenPipeError, EOFError, OSError) as exc:
                await self._mark_disconnected(exc)
                raise FunctionWorkerDisconnected(
                    f"{self.label} send failed: {type(exc).__name__}: {exc}"
                ) from exc

    async def _reader_loop(self) -> None:
        error: BaseException | None = None
        try:
            while not self._closing:
                # ``asyncio.to_thread(connection.recv_bytes)`` cannot be
                # cancelled once the OS read has started.  A closed peer could
                # therefore leave an executor thread blocked forever and make
                # both Worker shutdown and interpreter teardown hang.  Poll in
                # a bounded slice before receiving so cancellation/close is
                # observed without relying on cross-thread fd interruption.
                readable = await asyncio.to_thread(
                    self.connection.poll,
                    READER_POLL_INTERVAL_SECONDS,
                )
                if not readable:
                    continue
                payload = self.connection.recv_bytes(MAX_FRAME_BYTES)
                envelope = decode_envelope(payload)
                kind = envelope["kind"]
                if kind == RESPONSE:
                    self._receive_response(envelope)
                    continue
                task = asyncio.create_task(
                    self._dispatch(envelope),
                    name=f"worker-ipc-dispatch:{self.label}:{kind}",
                )
                self._dispatch_tasks.add(task)
                task.add_done_callback(self._dispatch_done)
        except asyncio.CancelledError:
            raise
        except (EOFError, BrokenPipeError, OSError, FunctionWorkerProtocolError) as exc:
            error = exc
        finally:
            await self._mark_disconnected(error)

    def _dispatch_done(self, task: asyncio.Task[Any]) -> None:
        self._dispatch_tasks.discard(task)
        if task.cancelled() or self._closing:
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            asyncio.create_task(self._mark_disconnected(error))

    def _receive_response(self, envelope: Mapping[str, Any]) -> None:
        pending = self._pending.get(str(envelope["id"]))
        if pending is None:
            return
        method, future = pending
        if future.done():
            return
        if envelope["ok"]:
            future.set_result(envelope.get("result"))
        else:
            future.set_exception(
                FunctionWorkerRemoteError(method, envelope.get("error") or {})
            )

    async def _dispatch(self, envelope: Mapping[str, Any]) -> None:
        if envelope["kind"] == EVENT:
            if self.event_handler is None:
                return
            result = self.event_handler(
                str(envelope["event"]),
                dict(envelope.get("payload") or {}),
            )
            if inspect.isawaitable(result):
                await result
            return

        request_id = str(envelope["id"])
        method = str(envelope["method"])
        if self.request_handler is None:
            await self._send_error(
                request_id,
                error_type="MethodNotFound",
                message=f"no request handler is installed for {method!r}",
            )
            return
        try:
            result = self.request_handler(method, dict(envelope.get("params") or {}))
            if inspect.isawaitable(result):
                result = await result
            await self._send(
                {
                    "version": FUNCTION_WORKER_PROTOCOL_VERSION,
                    "kind": RESPONSE,
                    "id": request_id,
                    "ok": True,
                    "result": json_value(result),
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._send_error(
                request_id,
                error_type=type(exc).__name__,
                message=str(exc) or type(exc).__name__,
            )

    async def _send_error(
        self,
        request_id: str,
        *,
        error_type: str,
        message: str,
    ) -> None:
        await self._send(
            {
                "version": FUNCTION_WORKER_PROTOCOL_VERSION,
                "kind": RESPONSE,
                "id": request_id,
                "ok": False,
                "error": {
                    "type": str(error_type),
                    "message": str(message)[:4000],
                },
            }
        )

    async def _mark_disconnected(self, error: BaseException | None) -> None:
        if self._closed.is_set():
            return
        self._disconnect_error = error
        self._closing = True
        disconnect = FunctionWorkerDisconnected(
            f"{self.label} disconnected"
            + (
                f": {type(error).__name__}: {error}"
                if error is not None
                else ""
            )
        )
        for _method, future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(disconnect)
        self._pending.clear()
        self._closed.set()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def close(self) -> None:
        if self._closing and self._closed.is_set():
            return
        self._closing = True
        try:
            self.connection.close()
        except (OSError, ValueError):
            pass
        reader = self._reader_task
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):
                pass
        for task in tuple(self._dispatch_tasks):
            task.cancel()
        if self._dispatch_tasks:
            await asyncio.gather(*self._dispatch_tasks, return_exceptions=True)
        await self._mark_disconnected(None)
