"""Closed private DTO codec and role dispatch, never Python object deserialization."""
from __future__ import annotations

import base64
from dataclasses import asdict, fields, is_dataclass
import json
import inspect

from adapters.stream_events import StreamEvent
from adapters.base import TokenUsage
from tools.registry import ToolResult

from orchestrator.permissions import authority, models
from orchestrator.permissions.enterprise_decisions import SessionDecisionOutcome
from orchestrator.permissions.execution_gate import GateResult, SupervisedResult

_TYPES = {
    cls.__name__: cls for cls in (
        models.Scope, models.ExecutionContext, models.RuntimeExecutionBinding, models.PermissionSubject,
        authority.AccessRequest, authority.PermissionLease, authority.ExecutionClaim,
        authority.ExecutionStatus, authority.OperationStatus, authority.SessionRequestView,
        authority.SessionRequestPage, authority.DecisionAction,
        GateResult, SupervisedResult, SessionDecisionOutcome, StreamEvent, TokenUsage, ToolResult,
    )
}
_TAG = "__hashi_permission_dto_v1__"


class PermissionRemoteError(RuntimeError):
    def __init__(self, code):
        self.code = str(code)
        super().__init__("permission service: " + self.code)


def encode(value):
    if is_dataclass(value) and not isinstance(value, type):
        if type(value).__name__ not in _TYPES or _TYPES[type(value).__name__] is not type(value):
            raise ValueError("unregistered permission wire type")
        return {_TAG: type(value).__name__, "fields": {f.name: encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, bytes):
        return {_TAG: "bytes", "data": base64.b64encode(value).decode("ascii")}
    if isinstance(value, tuple):
        return {_TAG: "tuple", "items": [encode(item) for item in value]}
    if isinstance(value, list):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        # Dictionary entries are explicit so a model's own argument named like
        # a DTO tag remains data and cannot turn into an internal object.
        if any(not isinstance(key, str) for key in value):
            raise ValueError("permission wire keys must be strings")
        return {_TAG: "mapping", "items": [[key, encode(item)] for key, item in value.items()]}
    if value is None or type(value) in (str, int, bool, float):
        json.dumps(value, allow_nan=False)
        return value
    raise ValueError("unsupported permission wire value")


def decode(value, *, _depth=0):
    if _depth > 64:
        raise ValueError("permission wire structure is too deep")
    if isinstance(value, list):
        return [decode(item, _depth=_depth + 1) for item in value]
    if not isinstance(value, dict):
        if value is None or type(value) in (str, int, bool, float):
            json.dumps(value, allow_nan=False)
            return value
        raise ValueError("invalid permission wire scalar")
    kind = value.get(_TAG)
    if kind == "mapping" and set(value) == {_TAG, "items"}:
        items = value["items"]
        if (not isinstance(items, list)
                or any(not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str) for pair in items)
                or len({pair[0] for pair in items}) != len(items)):
            raise ValueError("invalid permission wire mapping")
        return {key: decode(item, _depth=_depth + 1) for key, item in items}
    if kind == "tuple" and set(value) == {_TAG, "items"} and isinstance(value["items"], list):
        return tuple(decode(item, _depth=_depth + 1) for item in value["items"])
    if kind == "bytes" and set(value) == {_TAG, "data"} and isinstance(value["data"], str):
        return base64.b64decode(value["data"], validate=True)
    if not isinstance(kind, str):
        raise ValueError("invalid permission wire type name")
    cls = _TYPES.get(kind)
    if cls is None or set(value) != {_TAG, "fields"} or not isinstance(value["fields"], dict):
        raise ValueError("unknown permission wire type")
    raw = value["fields"]
    if set(raw) != {field.name for field in fields(cls)}:
        raise ValueError("permission DTO fields changed")
    return cls(**{key: decode(item, _depth=_depth + 1) for key, item in raw.items()})


def request_frame(method, arguments, *, request_id):
    if not isinstance(method, str) or not method or len(method) > 128:
        raise ValueError("a bounded permission method is required")
    return {"type": "hashi.permission-request.v1", "id": request_id, "method": method, "arguments": encode(arguments)}


class PermissionServiceRouter:
    """One OS-authenticated role and fixed receiver, never a caller-owned role."""

    def __init__(self, receiver, *, methods):
        allowed = frozenset(methods)
        if not allowed or not all(isinstance(name, str) and callable(getattr(receiver, name, None)) for name in allowed):
            raise ValueError("a closed trusted method surface is required")
        self._receiver, self._methods = receiver, allowed

    def _invocation(self, frame):
        if (not isinstance(frame, dict) or set(frame) != {"type", "id", "method", "arguments"}
                or frame["type"] != "hashi.permission-request.v1"
                or not isinstance(frame["id"], str) or not 1 <= len(frame["id"]) <= 128):
            raise ValueError("invalid permission request frame")
        method = frame["method"]
        if not isinstance(method, str) or method not in self._methods:
            raise PermissionError("permission method is outside this OS role")
        arguments = decode(frame["arguments"])
        if not isinstance(arguments, dict):
            raise ValueError("permission arguments must be an object")
        return getattr(self._receiver, method), arguments

    def dispatch(self, frame):
        method, arguments = self._invocation(frame)
        if inspect.iscoroutinefunction(method):
            raise TypeError("async permission methods require the async dispatcher")
        result = method(**arguments)
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError("async permission methods require the async dispatcher")
        return {"type": "hashi.permission-result.v1", "id": frame["id"], "result": encode(result)}

    async def dispatch_async(self, frame):
        method, arguments = self._invocation(frame)
        result = method(**arguments)
        if inspect.isawaitable(result):
            result = await result
        return {"type": "hashi.permission-result.v1", "id": frame["id"], "result": encode(result)}


def dto_mapping(value):
    """For private artifact metadata, never a deserializer or authorization."""
    return asdict(value)
