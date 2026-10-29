"""Fixed Run-bound HERV3 ports, retaining their existing Functions owners.

This is not a general Functions RPC. A host can invoke only these presentation,
learning and ordinary-question operations for its already accepted Turn. No
configuration writer, approval, filesystem path loader or import name exists.
"""
from __future__ import annotations

import inspect
import json

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.host_protocol import pack, unpack


# Fixed operation -> actual Runtime dependency and method. Callers cannot name
# another object or attribute; normal Python signatures still validate arguments.
_OPERATIONS = {
    "commentary": ("commentary", "publish"),
    "draft": ("commentary", "publish_draft"),
    "required_persona": ("required_persona", "render"),
    "final_style": ("final_style", "render"),
    "habits": ("habits", "retrieve"),
    "meditation": ("meditation", "meditate"),
    "turn_close": ("turn_services", "close"),
    "tool_started": ("turn_services", "tool_started"),
    "tool_completed": ("turn_services", "tool_completed"),
    "tool_activity": ("turn_services", "observe_tool_activity"),
    "intervention": ("turn_services", "take_intervention"),
    "snapshot": ("turn_services", "snapshot"),
}


def manifest(runtime):
    available = []
    for name, (attribute, method) in _OPERATIONS.items():
        if callable(getattr(getattr(runtime, attribute, None), method, None)):
            available.append(name)
    return {
        "operations": available,
        "allow_immediate_response": bool(getattr(runtime.delivery, "allow_immediate_response", False)),
        "workzone_ref": runtime.workzone_ref,
    }


async def invoke(turn, operation, arguments):
    runtime = getattr(turn, "runtime", None)
    if runtime is None:
        raise PermissionDenied("the actual HER runtime owner is unavailable")
    if operation == "manifest":
        if arguments != {"args": [], "kwargs": {}}:
            raise PermissionDenied("runtime manifest takes no arguments")
        return pack(manifest(runtime))
    if (operation not in _OPERATIONS or not isinstance(arguments, dict)
            or set(arguments) != {"args", "kwargs"}
            or not isinstance(arguments["args"], list) or not isinstance(arguments["kwargs"], dict)):
        raise PermissionDenied("unknown fixed HER runtime port")
    if len(json.dumps(arguments, allow_nan=False)) > 2_000_000:
        raise PermissionDenied("HER runtime port payload exceeds its bound")
    attribute, method = _OPERATIONS[operation]
    target = getattr(getattr(runtime, attribute, None), method, None)
    if not callable(target):
        raise PermissionDenied("the original HER runtime port is unavailable")
    # Packed arguments preserve only the closed DTO/enum set, never callables.
    args = [unpack(value) for value in arguments["args"]]
    kwargs = {key: unpack(value) for key, value in arguments["kwargs"].items()}
    inspect.signature(target).bind(*args, **kwargs)
    turn_id = turn.binding.context.turn_id
    if kwargs.get("turn_id", turn_id) != turn_id:
        raise PermissionDenied("runtime port belongs to another HER Turn")
    for value in args:
        if getattr(value, "turn_id", turn_id) != turn_id:
            raise PermissionDenied("presentation port belongs to another HER Turn")
    result = target(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return pack(result)


class HostRuntimePorts:
    def __init__(self, client):
        self.client = client
        self.manifest = self._sync("manifest")
        if (not isinstance(self.manifest, dict)
                or set(self.manifest) != {"operations", "allow_immediate_response", "workzone_ref"}
                or not isinstance(self.manifest["operations"], list)
                or set(self.manifest["operations"]) - _OPERATIONS.keys()):
            raise ValueError("invalid bound HERV3 runtime ports")

    @staticmethod
    def _arguments(args, kwargs):
        return {"args": [pack(value) for value in args],
                "kwargs": {key: pack(value) for key, value in kwargs.items()}}

    def _sync(self, operation, *args, **kwargs):
        return unpack(self.client.call("runtime_port", {
            "operation": operation, "arguments": self._arguments(args, kwargs),
        }))

    async def _async(self, operation, *args, **kwargs):
        return unpack(await self.client.call_async("runtime_port", {
            "operation": operation, "arguments": self._arguments(args, kwargs),
        }))

    def port(self, operation):
        return _AsyncPort(self, operation) if operation in self.manifest["operations"] else None

    def services(self):
        return _TurnServicesPort(self) if "snapshot" in self.manifest["operations"] else None


class _AsyncPort:
    def __init__(self, owner, operation):
        self.owner, self.operation = owner, operation

    async def render(self, *args, **kwargs):
        return await self.owner._async(self.operation, *args, **kwargs)

    async def retrieve(self, **kwargs):
        return await self.owner._async("habits", **kwargs)

    async def meditate(self, **kwargs):
        return await self.owner._async("meditation", **kwargs)

    async def publish(self, commentary):
        return await self.owner._async("commentary", commentary)

    async def publish_draft(self, commentary):
        return await self.owner._async("draft", commentary)


class _TurnServicesPort(_AsyncPort):
    def __init__(self, owner):
        super().__init__(owner, "commentary")

    async def close(self):
        return await self.owner._async("turn_close")

    def tool_started(self, *args, **kwargs):
        return self.owner._sync("tool_started", *args, **kwargs)

    def tool_completed(self, *args, **kwargs):
        return self.owner._sync("tool_completed", *args, **kwargs)

    def observe_tool_activity(self, facts):
        return self.owner._sync("tool_activity", facts)

    def take_intervention(self):
        return self.owner._sync("intervention")

    def snapshot(self):
        return self.owner._sync("snapshot")
