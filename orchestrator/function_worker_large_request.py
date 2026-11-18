"""Bounded transfer for large logical Worker requests over the private IPC.

Each temporary upload belongs to one Worker host/private peer.  No filesystem
path or owner claim is accepted as a reference.  A request is dispatched only
after all bytes, the digest and JSON have been verified.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping
from uuid import uuid4

from orchestrator.function_worker_protocol import (
    FunctionWorkerProtocolError,
    FunctionWorkerRemoteError,
    MAX_FRAME_BYTES,
    json_value,
)

LARGE_REQUEST_VERSION = 1
CHUNK_BYTES = 512 * 1024
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_TRANSFERS = 8
DEFAULT_IDLE_SECONDS = 120.0
DEFAULT_TRANSFER_TIMEOUT_SECONDS = 180
ALLOWED_METHODS = frozenset({"runtime.enqueue_request", "runtime.enqueue_api_text"})
BEGIN = "runtime.large_request.begin"
APPEND = "runtime.large_request.append"
COMMIT = "runtime.large_request.commit"
ABORT = "runtime.large_request.abort"
CONTROL_METHODS = frozenset({BEGIN, APPEND, COMMIT, ABORT})
CORE_CONTROLS = {
    BEGIN: "core.large_request.begin",
    APPEND: "core.large_request.append",
    COMMIT: "core.large_request.commit",
    ABORT: "core.large_request.abort",
}
CORE_CONTROL_ALIASES = {value: key for key, value in CORE_CONTROLS.items()}
_ID = re.compile(r"^[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _method_allowed(method: Any, *, allow_core_methods: bool) -> bool:
    if not isinstance(method, str):
        return False
    if allow_core_methods:
        return method.startswith("core.") and method not in CORE_CONTROL_ALIASES
    return method in ALLOWED_METHODS


def _positive_setting(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise FunctionWorkerProtocolError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise FunctionWorkerProtocolError(f"{name} must be a positive integer")
    return value


def _transfer_timeout() -> float:
    return float(_positive_setting(
        "HASHI_FUNCTION_LARGE_TRANSFER_TIMEOUT_SECONDS", DEFAULT_TRANSFER_TIMEOUT_SECONDS
    ))


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key in large request")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite number in large request: {value}")


@dataclass
class _Upload:
    method: str
    size: int
    digest: str
    stream: Any
    hasher: Any
    offset: int = 0
    expiry: asyncio.TimerHandle | None = None


class LargeRequestReceiver:
    """Spool a limited set of incomplete uploads tied to this Worker host."""

    def __init__(self, *, max_bytes: int | None = None,
                 max_transfers: int | None = None,
                 idle_seconds: float | None = None,
                 allow_core_methods: bool = False) -> None:
        self.allow_core_methods = bool(allow_core_methods)
        self.max_bytes = (
            _positive_setting("HASHI_FUNCTION_LARGE_REQUEST_MAX_BYTES", DEFAULT_MAX_BYTES)
            if max_bytes is None else int(max_bytes)
        )
        self.max_transfers = (
            _positive_setting("HASHI_FUNCTION_LARGE_REQUEST_MAX_TRANSFERS", DEFAULT_MAX_TRANSFERS)
            if max_transfers is None else int(max_transfers)
        )
        self.idle_seconds = float(
            _positive_setting("HASHI_FUNCTION_LARGE_TRANSFER_IDLE_SECONDS", int(DEFAULT_IDLE_SECONDS))
            if idle_seconds is None else idle_seconds
        )
        if self.max_bytes <= 0 or self.max_transfers <= 0 or self.idle_seconds <= 0:
            raise ValueError("large request resource settings must be positive")
        self._uploads: dict[str, _Upload] = {}
        self._reserved_bytes = 0
        self._abandoned: dict[str, asyncio.TimerHandle] = {}
        self._closed = False

    def capability(self) -> dict[str, int | float]:
        return {
            "version": LARGE_REQUEST_VERSION,
            "max_spooled_bytes": self.max_bytes,
            "max_inflight_transfers": self.max_transfers,
            "chunk_bytes": CHUNK_BYTES,
            "idle_timeout_seconds": self.idle_seconds,
            "transfer_timeout_seconds": _transfer_timeout(),
        }

    def _drop(self, transfer_id: str) -> _Upload | None:
        upload = self._uploads.pop(transfer_id, None)
        if upload is not None:
            self._reserved_bytes -= upload.size
            if upload.expiry is not None:
                upload.expiry.cancel()
            upload.stream.close()
        return upload

    def _refresh_expiry(self, transfer_id: str, upload: _Upload) -> None:
        if upload.expiry is not None:
            upload.expiry.cancel()
        upload.expiry = asyncio.get_running_loop().call_later(
            self.idle_seconds, self._drop, transfer_id
        )

    def close(self) -> None:
        self._closed = True
        for transfer_id in tuple(self._uploads):
            self._drop(transfer_id)
        for timer in self._abandoned.values():
            timer.cancel()
        self._abandoned.clear()

    def _forget_abandoned(self, transfer_id: str) -> None:
        self._abandoned.pop(transfer_id, None)

    @staticmethod
    def _transfer_id(value: Any) -> str:
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise FunctionWorkerProtocolError("invalid large request transfer identifier")
        return value

    def _begin(self, params: Mapping[str, Any]) -> dict[str, int]:
        transfer_id = self._transfer_id(params.get("transfer_id"))
        if self._closed or transfer_id in self._abandoned:
            raise FunctionWorkerProtocolError("large request transfer was cancelled or is unavailable")
        method = params.get("method")
        size = params.get("size")
        digest = params.get("sha256")
        if not _method_allowed(method, allow_core_methods=self.allow_core_methods):
            raise FunctionWorkerProtocolError("unsupported large request method")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise FunctionWorkerProtocolError("invalid large request length")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise FunctionWorkerProtocolError("invalid large request digest")
        if transfer_id in self._uploads:
            raise FunctionWorkerProtocolError("large request transfer already exists")
        if size > self.max_bytes or self._reserved_bytes + size > self.max_bytes:
            raise FunctionWorkerProtocolError(
                f"large request exceeds configured logical byte budget ({self.max_bytes})"
            )
        if len(self._uploads) >= self.max_transfers:
            raise FunctionWorkerProtocolError("large request transfer capacity reached")
        stream = tempfile.TemporaryFile(mode="w+b")
        upload = _Upload(str(method), size, digest, stream, hashlib.sha256())
        self._uploads[transfer_id] = upload
        self._reserved_bytes += size
        self._refresh_expiry(transfer_id, upload)
        return {"version": LARGE_REQUEST_VERSION, "chunk_bytes": CHUNK_BYTES}

    def _append(self, params: Mapping[str, Any]) -> dict[str, int]:
        transfer_id = self._transfer_id(params.get("transfer_id"))
        upload = self._uploads.get(transfer_id)
        if upload is None:
            raise FunctionWorkerProtocolError("large request transfer is unavailable")
        try:
            offset, text = params.get("offset"), params.get("data")
            if (not isinstance(offset, int) or isinstance(offset, bool)
                    or offset != upload.offset):
                raise FunctionWorkerProtocolError("large request chunk is out of order")
            if not isinstance(text, str) or len(text) > 4 * ((CHUNK_BYTES + 2) // 3):
                raise FunctionWorkerProtocolError("large request chunk is invalid or too large")
            try:
                data = base64.b64decode(text, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise FunctionWorkerProtocolError("large request chunk is not valid base64") from exc
            if not data or len(data) > CHUNK_BYTES or upload.offset + len(data) > upload.size:
                raise FunctionWorkerProtocolError("large request chunk length is invalid")
            written = upload.stream.write(data)
            if written != len(data):
                raise FunctionWorkerProtocolError("large request chunk could not be stored")
            upload.hasher.update(data)
            upload.offset += len(data)
            self._refresh_expiry(transfer_id, upload)
            return {"offset": upload.offset}
        except BaseException:
            self._drop(transfer_id)
            raise

    def _consume(self, params: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        transfer_id = self._transfer_id(params.get("transfer_id"))
        upload = self._uploads.get(transfer_id)
        if upload is None:
            raise FunctionWorkerProtocolError("large request transfer is unavailable")
        try:
            if upload.offset != upload.size or upload.hasher.hexdigest() != upload.digest:
                raise FunctionWorkerProtocolError("large request length or digest mismatch")
            upload.stream.seek(0)
            raw = upload.stream.read(upload.size + 1)
            if len(raw) != upload.size or hashlib.sha256(raw).hexdigest() != upload.digest:
                raise FunctionWorkerProtocolError("stored large request length or digest mismatch")
            try:
                decoded = json.loads(
                    raw.decode("utf-8"), object_pairs_hook=_strict_object,
                    parse_constant=_reject_constant,
                )
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                raise FunctionWorkerProtocolError("large request is not valid UTF-8 JSON") from exc
            if not isinstance(decoded, dict):
                raise FunctionWorkerProtocolError("large request parameters must be an object")
            return upload.method, decoded
        finally:
            self._drop(transfer_id)

    async def handle(self, method: str, params: Mapping[str, Any],
                     dispatch: Callable[[str, dict[str, Any]], Awaitable[Any]]) -> Any:
        if method == BEGIN:
            return self._begin(params)
        if method == APPEND:
            return self._append(params)
        if method == ABORT:
            transfer_id = self._transfer_id(params.get("transfer_id"))
            removed = self._drop(transfer_id) is not None
            if not self._closed and transfer_id not in self._abandoned:
                self._abandoned[transfer_id] = asyncio.get_running_loop().call_later(
                    self.idle_seconds, self._forget_abandoned, transfer_id
                )
            return {"removed": removed}
        if method == COMMIT:
            target, decoded = self._consume(params)
            return await dispatch(target, decoded)
        raise FunctionWorkerProtocolError("unknown large request operation")


async def send_large_request(
    call: Callable[..., Awaitable[Any]],
    method: str,
    params: Mapping[str, Any] | None,
    *,
    timeout: float,
    allow_core_methods: bool = False,
) -> Any:
    """Send a request known not to have fit in a single unsent IPC frame."""
    if not _method_allowed(method, allow_core_methods=allow_core_methods):
        raise FunctionWorkerProtocolError("unsupported large request method")
    controls = CORE_CONTROLS if allow_core_methods else {
        name: name for name in CONTROL_METHODS
    }
    try:
        payload = json.dumps(
            json_value(dict(params or {})), ensure_ascii=False, allow_nan=False,
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FunctionWorkerProtocolError("large request is not valid JSON") from exc
    transfer_timeout = _transfer_timeout()
    transfer_id = uuid4().hex
    begin_attempted = False
    committed = False
    try:
        async with asyncio.timeout(transfer_timeout):
            begin_attempted = True
            try:
                offered = await call(controls[BEGIN], {
                    "transfer_id": transfer_id, "method": method,
                    "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                }, timeout=transfer_timeout)
            except FunctionWorkerRemoteError as exc:
                if (exc.error.get("type") == "FunctionWorkerStateError"
                        and exc.error.get("message") == f"unknown Worker request: {BEGIN}"):
                    raise FunctionWorkerProtocolError(
                        "active Worker does not support bounded large requests; "
                        "update it before retrying"
                    ) from exc
                raise
            if not isinstance(offered, Mapping) or offered.get("version") != LARGE_REQUEST_VERSION:
                raise FunctionWorkerProtocolError("Worker does not support the large request contract")
            chunk_size = offered.get("chunk_bytes")
            if (not isinstance(chunk_size, int) or isinstance(chunk_size, bool)
                    or chunk_size <= 0 or chunk_size > CHUNK_BYTES):
                raise FunctionWorkerProtocolError("Worker offered an invalid large request chunk size")
            for offset in range(0, len(payload), chunk_size):
                chunk = payload[offset:offset + chunk_size]
                accepted = await call(controls[APPEND], {
                    "transfer_id": transfer_id, "offset": offset,
                    "data": base64.b64encode(chunk).decode("ascii"),
                }, timeout=transfer_timeout)
                if not isinstance(accepted, Mapping) or accepted.get("offset") != offset + len(chunk):
                    raise FunctionWorkerProtocolError("Worker did not acknowledge the complete chunk")
        try:
            result = await call(controls[COMMIT], {"transfer_id": transfer_id}, timeout=timeout)
        except FunctionWorkerRemoteError as exc:
            # Preserve the original method for existing error handling.
            raise FunctionWorkerRemoteError(method, exc.error) from exc
        committed = True
        return result
    finally:
        if begin_attempted and not committed:
            async def abort() -> None:
                try:
                    await asyncio.wait_for(call(controls[ABORT], {"transfer_id": transfer_id}, timeout=3), 3)
                except Exception:
                    pass
            cleanup = asyncio.create_task(abort())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # The receiver also expires abandoned uploads and closes them
                # when the Worker exits, even after repeated caller cancellation.
                pass


class BoundedWorkerPeer:
    """Use bounded uploads for oversized Worker-to-shared-Functions requests."""

    def __init__(self, peer: Any, topology: Mapping[str, Any] | None = None) -> None:
        self._peer = peer
        self.update_topology(topology or {})

    def __getattr__(self, name: str) -> Any:
        return getattr(self._peer, name)

    def update_topology(self, topology: Mapping[str, Any]) -> None:
        advertised = topology.get("worker_large_requests_to_core", {})
        self._supports_large = (
            isinstance(advertised, Mapping)
            and type(advertised.get("version")) is int
            and advertised["version"] == LARGE_REQUEST_VERSION
        )

    async def request(self, method: str, params: Mapping[str, Any] | None = None,
                      *, timeout: float = 120.0) -> Any:
        try:
            return await self._peer.request(method, params, timeout=timeout)
        except FunctionWorkerProtocolError as exc:
            if (type(exc) is not FunctionWorkerProtocolError
                    or not self._supports_large
                    or not _method_allowed(method, allow_core_methods=True)
                    or not str(exc).startswith(f"IPC frame exceeds {MAX_FRAME_BYTES} bytes:")):
                raise
            # The protected encoder rejected this local frame before sending.
            # Never retry a remote error or an uncertain invocation.
            return await send_large_request(
                self._peer.request, method, params, timeout=timeout,
                allow_core_methods=True,
            )


CAPABILITY_PROBE = "core.capability.large_result.probe"
CAPABILITY_INVOKE = "core.capability.large_result.invoke"
CAPABILITY_READ = "core.capability.large_result.read"
CAPABILITY_RELEASE = "core.capability.large_result.release"
CAPABILITY_INLINE_BYTES = 8 * 1024 * 1024


@dataclass
class _Result:
    size: int
    digest: str
    stream: Any
    offset: int = 0
    expiry: asyncio.TimerHandle | None = None


class LargeCapabilityResultStore:
    """Temporary results bound to one authenticated Worker connection."""

    def __init__(self, *, max_bytes: int | None = None,
                 max_transfers: int | None = None,
                 idle_seconds: float | None = None) -> None:
        self.max_bytes = (
            _positive_setting("HASHI_FUNCTION_LARGE_RESULT_MAX_BYTES", DEFAULT_MAX_BYTES)
            if max_bytes is None else int(max_bytes)
        )
        self.max_transfers = (
            _positive_setting("HASHI_FUNCTION_LARGE_RESULT_MAX_TRANSFERS", DEFAULT_MAX_TRANSFERS)
            if max_transfers is None else int(max_transfers)
        )
        self.idle_seconds = float(
            _positive_setting("HASHI_FUNCTION_LARGE_TRANSFER_IDLE_SECONDS", int(DEFAULT_IDLE_SECONDS))
            if idle_seconds is None else idle_seconds
        )
        if self.max_bytes <= 0 or self.max_transfers <= 0 or self.idle_seconds <= 0:
            raise ValueError("large result resource settings must be positive")
        self._results: dict[str, _Result] = {}
        self._reserved_bytes = 0
        self._pending: set[str] = set()
        self._abandoned: dict[str, asyncio.TimerHandle] = {}
        self._closed = False

    def capability(self) -> dict[str, int | float]:
        return {"version": LARGE_REQUEST_VERSION, "max_spooled_bytes": self.max_bytes,
                "max_inflight_transfers": self.max_transfers, "chunk_bytes": CHUNK_BYTES,
                "idle_timeout_seconds": self.idle_seconds,
                "transfer_timeout_seconds": _transfer_timeout()}

    def _drop(self, transfer_id: str) -> _Result | None:
        result = self._results.pop(transfer_id, None)
        if result is not None:
            self._reserved_bytes -= result.size
            if result.expiry is not None:
                result.expiry.cancel()
            result.stream.close()
        return result

    def _refresh_expiry(self, transfer_id: str, result: _Result) -> None:
        if result.expiry is not None:
            result.expiry.cancel()
        result.expiry = asyncio.get_running_loop().call_later(
            self.idle_seconds, self._drop, transfer_id
        )

    def close(self) -> None:
        self._closed = True
        for transfer_id in tuple(self._results):
            self._drop(transfer_id)
        self._pending.clear()
        for timer in self._abandoned.values():
            timer.cancel()
        self._abandoned.clear()

    def begin(self, value: Any) -> str:
        transfer_id = LargeRequestReceiver._transfer_id(value)
        if (self._closed or transfer_id in self._pending
                or transfer_id in self._results or transfer_id in self._abandoned):
            raise FunctionWorkerProtocolError("capability result request is unavailable or repeated")
        self._pending.add(transfer_id)
        return transfer_id

    def finish(self, transfer_id: str) -> None:
        self._pending.discard(transfer_id)

    def _forget_abandoned(self, transfer_id: str) -> None:
        self._abandoned.pop(transfer_id, None)

    def publish(self, transfer_id: str, value: Any) -> dict[str, Any]:
        if self._closed or transfer_id not in self._pending:
            raise FunctionWorkerProtocolError("capability result receiver is no longer waiting")
        self._pending.remove(transfer_id)
        normalized = json_value(value)
        try:
            payload = json.dumps(
                normalized, ensure_ascii=False, allow_nan=False,
                separators=(",", ":"), sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise FunctionWorkerProtocolError("capability result is not valid JSON") from exc
        if len(payload) <= CAPABILITY_INLINE_BYTES:
            return {"kind": "inline", "value": normalized}
        if len(payload) > self.max_bytes or self._reserved_bytes + len(payload) > self.max_bytes:
            raise FunctionWorkerProtocolError(
                f"capability result exceeds configured logical byte budget ({self.max_bytes}); "
                "the invocation may already have completed"
            )
        if len(self._results) >= self.max_transfers:
            raise FunctionWorkerProtocolError(
                "capability result transfer capacity reached; the invocation may already have completed"
            )
        stream = tempfile.TemporaryFile(mode="w+b")
        try:
            if stream.write(payload) != len(payload):
                raise FunctionWorkerProtocolError("capability result could not be stored")
            stream.seek(0)
            digest = hashlib.sha256(payload).hexdigest()
            result = _Result(len(payload), digest, stream)
            self._results[transfer_id] = result
            self._reserved_bytes += len(payload)
            self._refresh_expiry(transfer_id, result)
        except BaseException:
            stream.close()
            raise
        return {"kind": "reference", "transfer_id": transfer_id, "size": len(payload),
                "sha256": digest, "chunk_bytes": CHUNK_BYTES}

    def read(self, params: Mapping[str, Any]) -> dict[str, Any]:
        transfer_id = LargeRequestReceiver._transfer_id(params.get("transfer_id"))
        result = self._results.get(transfer_id)
        if result is None:
            raise FunctionWorkerProtocolError("capability result reference is unavailable")
        offset = params.get("offset")
        if (not isinstance(offset, int) or isinstance(offset, bool)
                or offset != result.offset or offset >= result.size):
            self._drop(transfer_id)
            raise FunctionWorkerProtocolError("capability result chunk is out of order")
        data = result.stream.read(min(CHUNK_BYTES, result.size - offset))
        if not data:
            self._drop(transfer_id)
            raise FunctionWorkerProtocolError("capability result is incomplete")
        result.offset += len(data)
        self._refresh_expiry(transfer_id, result)
        return {"offset": offset, "next_offset": result.offset,
                "data": base64.b64encode(data).decode("ascii")}

    def release(self, params: Mapping[str, Any]) -> dict[str, bool]:
        transfer_id = LargeRequestReceiver._transfer_id(params.get("transfer_id"))
        removed = self._drop(transfer_id) is not None
        if transfer_id in self._pending:
            self._pending.remove(transfer_id)
            removed = True
        if not removed and not self._closed and transfer_id not in self._abandoned:
            # Release can overtake dispatch if the caller is cancelled while
            # its first frame is still being sent. A short-lived tombstone
            # prevents that late invocation from publishing orphaned content.
            self._abandoned[transfer_id] = asyncio.get_running_loop().call_later(
                self.idle_seconds, self._forget_abandoned, transfer_id
            )
        return {"removed": removed}


async def receive_capability_result(
    call: Callable[..., Awaitable[Any]],
    params: Mapping[str, Any],
    *,
    timeout: float,
) -> Any:
    """Invoke once, then retrieve a large result without replaying the action."""
    transfer_timeout = _transfer_timeout()
    transfer_id: str | None = uuid4().hex
    attempted = False
    descriptor_received = False
    try:
        try:
            attempted = True
            descriptor = await call(
                CAPABILITY_INVOKE,
                {"transfer_id": transfer_id, "operation": dict(params)},
                timeout=timeout,
            )
            descriptor_received = True
        except FunctionWorkerRemoteError as exc:
            raise FunctionWorkerRemoteError("core.capability.invoke", exc.error) from exc
        if not isinstance(descriptor, Mapping):
            raise FunctionWorkerProtocolError("invalid capability result descriptor")
        if descriptor.get("kind") == "inline":
            if "value" not in descriptor:
                raise FunctionWorkerProtocolError("invalid inline capability result")
            transfer_id = None
            return descriptor["value"]
        if descriptor.get("kind") != "reference":
            raise FunctionWorkerProtocolError("unknown capability result descriptor")
        offered_id = LargeRequestReceiver._transfer_id(descriptor.get("transfer_id"))
        if offered_id != transfer_id:
            raise FunctionWorkerProtocolError("capability result reference does not match request")
        size = descriptor.get("size")
        digest = descriptor.get("sha256")
        chunk_size = descriptor.get("chunk_bytes")
        max_bytes = _positive_setting("HASHI_FUNCTION_LARGE_RESULT_MAX_BYTES", DEFAULT_MAX_BYTES)
        if (not isinstance(size, int) or isinstance(size, bool) or size <= 0
                or size > max_bytes or not isinstance(digest, str)
                or not _DIGEST.fullmatch(digest) or not isinstance(chunk_size, int)
                or isinstance(chunk_size, bool) or not 0 < chunk_size <= CHUNK_BYTES):
            raise FunctionWorkerProtocolError("invalid capability result reference")
        async with asyncio.timeout(transfer_timeout):
            hasher = hashlib.sha256()
            offset = 0
            with tempfile.TemporaryFile(mode="w+b") as stream:
                while offset < size:
                    response = await call(CAPABILITY_READ, {
                        "transfer_id": transfer_id, "offset": offset,
                    }, timeout=transfer_timeout)
                    if not isinstance(response, Mapping):
                        raise FunctionWorkerProtocolError("invalid capability result chunk")
                    text = response.get("data")
                    if (response.get("offset") != offset or not isinstance(text, str)
                            or len(text) > 4 * ((CHUNK_BYTES + 2) // 3)):
                        raise FunctionWorkerProtocolError("invalid capability result chunk")
                    try:
                        chunk = base64.b64decode(text, validate=True)
                    except (ValueError, binascii.Error) as exc:
                        raise FunctionWorkerProtocolError("invalid capability result chunk") from exc
                    next_offset = offset + len(chunk)
                    if (not chunk or len(chunk) > chunk_size or next_offset > size
                            or response.get("next_offset") != next_offset):
                        raise FunctionWorkerProtocolError("invalid capability result chunk length")
                    if stream.write(chunk) != len(chunk):
                        raise FunctionWorkerProtocolError("capability result could not be stored")
                    hasher.update(chunk)
                    offset = next_offset
                if hasher.hexdigest() != digest:
                    raise FunctionWorkerProtocolError("capability result digest mismatch")
                stream.seek(0)
                raw = stream.read(size + 1)
                if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
                    raise FunctionWorkerProtocolError("stored capability result digest mismatch")
                try:
                    return json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object,
                                      parse_constant=_reject_constant)
                except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                    raise FunctionWorkerProtocolError("capability result is not valid UTF-8 JSON") from exc
    except Exception as exc:
        # An explicit broker rejection retains its original meaning. A lost
        # reply, bad result or later transport failure does not prove that the
        # requested operation failed or that repeating it is safe.
        if not attempted or (isinstance(exc, FunctionWorkerRemoteError)
                             and not descriptor_received):
            raise
        safe_details = {
            "capability result digest mismatch",
            "stored capability result digest mismatch",
            "capability result is not valid UTF-8 JSON",
            "invalid capability result descriptor",
            "invalid capability result reference",
            "invalid capability result chunk",
            "invalid capability result chunk length",
        }
        detail = str(exc) if isinstance(exc, FunctionWorkerProtocolError) and str(exc) in safe_details else "result transport failed"
        raise FunctionWorkerProtocolError(
            f"Capability result unavailable ({detail}); the effect may have occurred; "
            "do not automatically retry. Reconcile the original operation."
        ) from exc
    finally:
        if attempted and transfer_id is not None:
            async def release() -> None:
                try:
                    await asyncio.wait_for(
                        call(CAPABILITY_RELEASE, {"transfer_id": transfer_id}, timeout=3), 3
                    )
                except Exception:
                    pass
            cleanup = asyncio.create_task(release())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                pass
