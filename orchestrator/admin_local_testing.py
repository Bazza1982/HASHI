from __future__ import annotations
import asyncio
import shlex
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from orchestrator.command_registry import runtime_command_map
from orchestrator.runtime_command_binding import COMMAND_BINDINGS


def _json_safe(value: Any):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return repr(value)


@dataclass
class _CaptureStore:
    messages: list[dict[str, Any]]

    async def capture_reply(self, text: str, **kwargs):
        self.messages.append(
            {
                "channel": "reply",
                "chat_id": None,
                "text": text,
                "meta": _json_safe(kwargs or {}),
            }
        )
        return SimpleNamespace(ok=True)

    async def capture_send(self, chat_id: int, text: str, **kwargs):
        self.messages.append(
            {
                "channel": "send",
                "chat_id": chat_id,
                "text": text,
                "meta": _json_safe(kwargs or {}),
            }
        )
        return SimpleNamespace(ok=True)


class _FakeMessage:
    def __init__(self, store: _CaptureStore, text: str):
        self._store = store
        self.text = text

    async def reply_text(self, text: str, **kwargs):
        return await self._store.capture_reply(text, **kwargs)


class _FakeUpdate:
    def __init__(self, user_id: int, chat_id: int, store: _CaptureStore, text: str):
        self.effective_user = SimpleNamespace(id=user_id)
        self.effective_chat = SimpleNamespace(id=chat_id)
        self.message = _FakeMessage(store, text)


def _split_command(command_line: str) -> tuple[str, list[str]]:
    raw = (command_line or "").strip()
    if not raw:
        return "", []
    if raw.startswith("/"):
        raw = raw[1:]
    try:
        parts = shlex.split(raw)
    except Exception:
        parts = raw.split()
    if not parts:
        return "", []
    return parts[0].lower(), parts[1:]


def supported_commands(runtime) -> list[str]:
    names = [binding.name for binding in COMMAND_BINDINGS]
    supported = []
    for name in names:
        if hasattr(runtime, f"cmd_{name}"):
            supported.append(name)
    supported.extend(runtime_command_map().keys())
    return sorted(set(supported))


async def execute_local_command(runtime, command_line: str, chat_id: int | None = None) -> dict[str, Any]:
    command_name, args = _split_command(command_line)
    if not command_name:
        return {"ok": False, "error": "empty command"}
    if command_name == "restart":
        return {
            "ok": False,
            "command": command_name,
            "args": args,
            "error": "/restart is human-only and cannot be invoked through the local admin API. Use /reboot for agent-driven recovery.",
        }

    method_name = f"cmd_{command_name}"
    method = getattr(runtime, method_name, None)
    registry_command = None
    if method is None:
        registry_command = runtime_command_map().get(command_name)
        if registry_command is None:
            return {
                "ok": False,
                "error": f"unknown command: {command_name}",
                "supported_commands": supported_commands(runtime),
            }

    store = _CaptureStore(messages=[])
    local_chat_id = chat_id or runtime.global_config.authorized_id
    update = _FakeUpdate(runtime.global_config.authorized_id, local_chat_id, store, command_line)
    context = SimpleNamespace(args=args)

    lock = getattr(runtime, "_local_admin_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(runtime, "_local_admin_lock", lock)

    async with lock:
        original_send_text = getattr(runtime, "_send_text", None)
        if original_send_text is not None:
            runtime._send_text = store.capture_send
        try:
            if registry_command is not None:
                await registry_command.callback(runtime, update, context)
            else:
                await method(update, context)
        except Exception as e:
            return {
                "ok": False,
                "command": command_name,
                "args": args,
                "messages": store.messages,
                "error": f"{type(e).__name__}: {e}",
            }
        finally:
            if original_send_text is not None:
                runtime._send_text = original_send_text

    return {
        "ok": True,
        "command": command_name,
        "args": args,
        "messages": store.messages,
    }
