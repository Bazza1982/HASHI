"""Validate an exact private provider Tool batch before durable continuation."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import copy
import json


def pending_tool_calls(messages: Sequence[Mapping]) -> list[dict]:
    if not isinstance(messages, (list, tuple)) or not messages:
        raise ValueError("private provider messages are required")
    active, settled, seen = {}, set(), set()
    for message in messages:
        if not isinstance(message, Mapping):
            raise ValueError("invalid private provider message")
        role = message.get("role")
        if role == "tool":
            identity = message.get("tool_call_id")
            if identity not in active or identity in settled:
                raise ValueError("orphan or duplicate Tool result")
            settled.add(identity)
            continue
        if active.keys() - settled:
            raise ValueError("a new message precedes unresolved Tool results")
        if role != "assistant" or not message.get("tool_calls"):
            continue
        calls = message["tool_calls"]
        if not isinstance(calls, list) or not calls:
            raise ValueError("invalid pending Tool batch")
        active, settled = {}, set()
        for call in calls:
            if not isinstance(call, Mapping):
                raise ValueError("invalid pending Tool call")
            identity, function = call.get("id"), call.get("function")
            if (not isinstance(identity, str) or not 1 <= len(identity) <= 512
                    or identity in seen or not isinstance(function, Mapping)
                    or not isinstance(function.get("name"), str) or not function["name"]):
                raise ValueError("ambiguous pending Tool identity")
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                arguments = json.loads(arguments or "{}")
            if not isinstance(arguments, Mapping):
                raise ValueError("pending Tool arguments must be an object")
            # Verify JSON representability without changing the exact saved
            # arguments; the authoritative operation digest is checked later.
            json.dumps(arguments, allow_nan=False)
            seen.add(identity)
            active[identity] = dict(call)
    pending = [copy.deepcopy(call) for identity, call in active.items() if identity not in settled]
    if not pending:
        raise ValueError("the checkpoint contains no pending Tool call")
    return pending
