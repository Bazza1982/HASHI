"""Execution host view: reviewed tools run only in the assigned trusted channel."""
from __future__ import annotations

import json
from pathlib import Path

from orchestrator.permissions.continuation import PermissionPark
from orchestrator.permissions.execution_gate import GateResult
from orchestrator.permissions.wire import PermissionRemoteError
from tools.registry import ToolRegistry, ToolResult


class BrokeredToolRegistry(ToolRegistry):
    permission_durable_wait = True

    def __init__(self, client, *, definitions, scratch_root):
        if not isinstance(definitions, list):
            raise ValueError("qualified tool definitions are required")
        names = []
        for definition in definitions:
            if not isinstance(definition, dict) or definition.get("type") != "function":
                raise ValueError("invalid qualified tool definition")
            name = definition.get("function", {}).get("name")
            if not isinstance(name, str) or not name or name in names:
                raise ValueError("invalid or duplicate qualified tool name")
            names.append(name)
        self._broker_names = tuple(names)
        self._broker_definitions = json.loads(json.dumps(definitions, allow_nan=False))
        self._broker = client
        self._event_sink = None
        root = Path(scratch_root)
        super().__init__(allowed_tools=[], workspace_dir=root, access_root=root, secrets={})

    def is_allowed(self, tool_name):
        return tool_name in self._broker_names

    def allowed_tool_names(self):
        return self._broker_names

    def get_tool_definitions(self, tiers=None):
        # The trusted registry already derives the allowed surface and ceiling.
        # Host-side tier filtering must not expose a local execution fallback.
        del tiers
        return json.loads(json.dumps(self._broker_definitions))

    async def execute(self, tool_name, arguments, tool_call_id=""):
        if not self.is_allowed(tool_name) or not isinstance(arguments, dict) or not tool_call_id:
            return ToolResult(tool_call_id=tool_call_id, output="This tool is unavailable in this execution channel.",
                              is_error=False, details={"control_disposition": "denied"})
        try:
            result = await self._broker.call_async("tool_execute", {
                "tool_name": tool_name, "arguments": arguments, "tool_call_id": tool_call_id,
            }, **({"on_event": self._event_sink} if self._event_sink is not None else {}))
        except (PermissionRemoteError, OSError, EOFError, TimeoutError) as exc:
            raise PermissionPark(GateResult("unavailable", "")) from exc
        if isinstance(result, GateResult):
            raise PermissionPark(result)
        if not isinstance(result, ToolResult):
            raise PermissionPark(GateResult("unknown", ""))
        return result
