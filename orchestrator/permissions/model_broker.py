"""Selected-model transport broker. Credentials and HTTP handles stay trusted."""
from __future__ import annotations

import asyncio
from dataclasses import fields
import json

from adapters.openrouter_api import _APIResult
from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.pao_run_fence import PaoRunFence

# This is an IPC payload boundary, not a second model/effort catalogue.
# Provider-hosted plugins, connectors, remote tool servers and arbitrary HTTP
# targets are intentionally outside the selected model inference capability.
_MODEL_FIELDS = frozenset({
    "model", "messages", "input", "tools", "tool_choice", "parallel_tool_calls",
    "stream", "stream_options", "temperature", "top_p", "top_k",
    "max_tokens", "max_completion_tokens", "max_output_tokens", "reasoning",
    "reasoning_effort", "thinking", "response_format", "frequency_penalty",
    "presence_penalty", "repetition_penalty", "seed", "stop", "user",
    "modalities", "audio", "provider", "store", "include",
})


def api_result_to_wire(result):
    if not isinstance(result, _APIResult):
        raise ValueError("a typed provider result is required")
    return {field.name: getattr(result, field.name) for field in fields(result)}


def api_result_from_wire(value):
    if not isinstance(value, dict) or set(value) != {field.name for field in fields(_APIResult)}:
        raise ValueError("provider result fields changed")
    return _APIResult(**value)


class SelectedModelBroker:
    def __init__(self, authority, binding, *, drivers, run_guard, max_parallel_calls=4, event_sink=None):
        if (not isinstance(drivers, dict) or not drivers or type(max_parallel_calls) is not int
                or not 1 <= max_parallel_calls <= 32):
            raise ValueError("qualified selected model drivers are required")
        if not isinstance(run_guard, PaoRunFence) or run_guard.binding != binding:
            raise ValueError("the exact canonical PAO model-call fence is required")
        self._run_guard = run_guard
        self._authority, self._binding = authority, binding
        self._drivers = dict(drivers)
        self._capacity = asyncio.Semaphore(max_parallel_calls)
        self._event_sink = event_sink
        self._active = set()

    def _current(self):
        status = self._authority.execution_status(self._binding.context)
        if status.task_state != "active" or status.binding_state != "active":
            raise PermissionDenied("selected model capability is inactive")

    async def provider_call(self, *, engine, model, payload, use_stream):
        if type(use_stream) is not bool or not isinstance(payload, dict) or set(payload) - _MODEL_FIELDS:
            raise PermissionDenied("model payload exceeds the inference capability")
        if payload.get("model") != model or not isinstance(model, str) or not isinstance(engine, str):
            raise PermissionDenied("selected model binding changed")
        driver = self._drivers.get((engine, model))
        if driver is None:
            raise PermissionDenied("provider or model is outside this task")
        tools = payload.get("tools", [])
        if not isinstance(tools, list):
            raise PermissionDenied("provider tool definitions are invalid")
        for tool in tools:
            if (not isinstance(tool, dict) or set(tool) != {"type", "function"} or tool["type"] != "function"
                    or not isinstance(tool["function"], dict)
                    or set(tool["function"]) - {"name", "description", "parameters", "strict"}):
                raise PermissionDenied("provider-hosted remote tools are outside this capability")
        # Freeze the request before the resource queue and reject NaN/opaque
        # objects. The broker never accepts caller headers or endpoint URLs.
        frozen = json.loads(json.dumps(payload, allow_nan=False))
        async with self._capacity:
            task = asyncio.current_task()
            with self._run_guard(self._binding.context):
                self._current()
                self._active.add(task)
            try:
                headers = driver._request_headers()
                if use_stream:
                    result = await driver._stream_api_once(frozen, headers, self._event_sink)
                else:
                    result = await driver._call_api_once(frozen, headers, None)
                with self._run_guard(self._binding.context):
                    self._current()
                return api_result_to_wire(result)
            finally:
                self._active.discard(task)

    async def close(self):
        tasks = tuple(task for task in self._active if task is not asyncio.current_task())
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5)
            if pending:
                raise OSError("selected model cleanup is unverified")
        for driver in set(self._drivers.values()):
            await driver.shutdown()
