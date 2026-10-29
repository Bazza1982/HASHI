"""Private authenticated IPC client and server handshake for permission roles."""
from __future__ import annotations

import asyncio
import base64
from contextvars import ContextVar
import inspect
import json
import os
import secrets
import threading
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from orchestrator.permissions.wire import PermissionRemoteError, decode, encode, request_frame
from orchestrator.permissions.windows_ipc import connect_pipe

_DOMAIN = b"hashi.permissions.ipc-server.v1\0"
_SINK = ContextVar("hashi_permission_private_stream_sink", default=None)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def welcome(hello, *, signing_key, service_generation, endpoint):
    if (not isinstance(hello, dict) or set(hello) != {"type", "challenge"}
            or hello["type"] != "hashi.permission-hello.v1"
            or not isinstance(hello["challenge"], str) or not 32 <= len(hello["challenge"]) <= 128):
        raise PermissionError("invalid permission service challenge")
    proof = {
        "type": "hashi.permission-welcome.v1", "challenge": hello["challenge"],
        "generation": service_generation, "endpoint": endpoint, "process_id": os.getpid(),
    }
    return {**proof, "signature": base64.b64encode(signing_key.sign(_DOMAIN + _canonical(proof))).decode("ascii")}


def verify_welcome(frame, *, public_key, challenge, endpoint):
    if not isinstance(public_key, Ed25519PublicKey) or not isinstance(frame, dict):
        raise PermissionError("a pinned service key is required")
    expected = {"type", "challenge", "generation", "endpoint", "process_id", "signature"}
    if (set(frame) != expected or frame["type"] != "hashi.permission-welcome.v1"
            or frame["challenge"] != challenge or frame["endpoint"] != endpoint
            or not isinstance(frame["generation"], str) or not frame["generation"]
            or type(frame["process_id"]) is not int or frame["process_id"] <= 0):
        raise PermissionError("permission service identity changed")
    try:
        signature = base64.b64decode(frame["signature"], validate=True)
        public_key.verify(signature, _DOMAIN + _canonical({k: v for k, v in frame.items() if k != "signature"}))
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise PermissionError("permission service signature is invalid") from exc
    return frame["generation"]


async def emit_private_event(event):
    sink = _SINK.get()
    if sink is None:
        raise RuntimeError("no private permission stream is bound")
    await sink(event)


async def serve_request(pipe, router, frame):
    identity = frame.get("id") if isinstance(frame, dict) else None

    async def emit(event):
        await asyncio.to_thread(pipe.send, {"type": "hashi.permission-event.v1", "id": identity, "event": encode(event)})

    token = _SINK.set(emit)
    try:
        result = await router.dispatch_async(frame)
        await asyncio.to_thread(pipe.send, result)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # No arguments, exception text, user session credential, key, or raw
        # backend request appears in this public error projection.
        code = getattr(exc, "code", None)
        if type(exc).__name__ in {"PermissionDenied", "SessionDecisionDenied", "InvalidDecision",
                                  "RevisionConflict", "OperationConflict", "StaleFencingToken"}:
            code = type(exc).__name__
        elif not isinstance(code, str) or len(code) > 128:
            code = "permission_service_unavailable"
        await asyncio.to_thread(pipe.send, {"type": "hashi.permission-error.v1", "id": identity, "code": code})
    finally:
        _SINK.reset(token)


class PermissionPipeClient:
    def __init__(self, endpoint, public_key, *, methods, idle_timeout_s=300):
        if not isinstance(public_key, Ed25519PublicKey):
            raise ValueError("a pinned permission service public key is required")
        self.endpoint, self.public_key = endpoint, public_key
        self.methods = frozenset(methods)
        self.idle_timeout_s = idle_timeout_s

    def call(self, method, arguments, *, on_event=None, _register=None):
        if not isinstance(method, str) or method not in self.methods:
            raise PermissionError("permission method is outside this client role")
        pipe = connect_pipe(self.endpoint)
        try:
            if _register is not None:
                _register(pipe)
            challenge = secrets.token_urlsafe(32)
            pipe.send({"type": "hashi.permission-hello.v1", "challenge": challenge})
            verify_welcome(pipe.receive(timeout_s=5), public_key=self.public_key,
                           challenge=challenge, endpoint=self.endpoint)
            identity = uuid4().hex
            pipe.send(request_frame(method, arguments, request_id=identity))
            while True:
                frame = pipe.receive(timeout_s=self.idle_timeout_s)
                if frame.get("id") != identity:
                    raise PermissionError("permission response correlation changed")
                kind = frame.get("type")
                if kind == "hashi.permission-result.v1" and set(frame) == {"type", "id", "result"}:
                    return decode(frame["result"])
                if kind == "hashi.permission-event.v1" and set(frame) == {"type", "id", "event"}:
                    if on_event is not None:
                        on_event(decode(frame["event"]))
                    continue
                if kind == "hashi.permission-error.v1" and set(frame) == {"type", "id", "code"}:
                    raise PermissionRemoteError(frame["code"])
                raise ValueError("invalid permission response")
        finally:
            pipe.close()

    async def call_async(self, method, arguments, *, on_event=None):
        loop = asyncio.get_running_loop()
        cancelled, lock = threading.Event(), threading.Lock()
        owned, callbacks = [], []

        def register(pipe):
            with lock:
                if cancelled.is_set():
                    pipe.close()
                    raise ConnectionAbortedError("permission call was cancelled before dispatch")
                owned.append(pipe)

        def consume(event):
            if cancelled.is_set():
                raise ConnectionAbortedError("permission call was cancelled")
            if on_event is not None:
                result = on_event(event)
                if inspect.isawaitable(result):
                    future = asyncio.run_coroutine_threadsafe(result, loop)
                    with lock:
                        callbacks.append(future)
                        if cancelled.is_set():
                            future.cancel()
                    future.result(timeout=self.idle_timeout_s)

        task = asyncio.create_task(asyncio.to_thread(
            self.call, method, arguments, on_event=consume, _register=register,
        ))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            with lock:
                for callback in callbacks:
                    callback.cancel()
                for pipe in owned:
                    pipe.close()
            done, _ = await asyncio.wait({task}, timeout=5)
            if task in done:
                try:
                    task.result()
                except BaseException:
                    pass
            else:
                def consume_late(future):
                    try:
                        future.result()
                    except BaseException:
                        pass
                task.add_done_callback(consume_late)
            # The service owns settlement. Client cancellation never retries an
            # operation whose request may already have reached the receiver.
            raise
