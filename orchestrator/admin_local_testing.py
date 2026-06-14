from __future__ import annotations
import asyncio
import shlex
from pathlib import Path
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from orchestrator.command_registry import runtime_command_map
from orchestrator.runtime_command_binding import COMMAND_BINDINGS
from orchestrator.slash_command_audit import (
    SlashCommandAuditSession,
    default_audit_path,
    is_supported_slash_command,
    looks_like_slash_command,
    parse_slash_command_text,
    resolve_handler_kind,
)


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


def _runtime_audit_path(runtime) -> Path:
    workspace_dir = getattr(runtime, "workspace_dir", None)
    if workspace_dir is None:
        config = getattr(runtime, "config", None)
        workspace_dir = getattr(config, "workspace_dir", None)
    if workspace_dir is None:
        bridge_home = getattr(getattr(runtime, "global_config", None), "bridge_home", None)
        agent_name = getattr(runtime, "name", "unknown")
        return Path(bridge_home or ".") / "workspaces" / str(agent_name) / "slash_command_audit.jsonl"
    return default_audit_path(Path(workspace_dir))


def _runtime_agent_name(runtime) -> str:
    return str(getattr(runtime, "name", None) or getattr(getattr(runtime, "config", None), "name", "unknown"))


async def try_execute_slash_command_text(
    runtime,
    text: str,
    *,
    source_channel: str = "api_chat",
    chat_id: int | str | None = None,
) -> dict[str, Any] | None:
    if not looks_like_slash_command(text):
        return None
    command_name, _args = parse_slash_command_text(text)
    if not is_supported_slash_command(runtime, command_name):
        return None
    return await execute_local_command(
        runtime,
        text.strip(),
        chat_id=chat_id,
        source_channel=source_channel,
    )


async def execute_local_command(
    runtime,
    command_line: str,
    chat_id: int | None = None,
    source_channel: str = "workbench_api",
) -> dict[str, Any]:
    command_name, args = _split_command(command_line)
    local_chat_id = chat_id or runtime.global_config.authorized_id
    actor_id = getattr(runtime.global_config, "authorized_id", None)
    session = SlashCommandAuditSession(
        audit_path=_runtime_audit_path(runtime),
        agent=_runtime_agent_name(runtime),
        command_name=command_name or "(empty)",
        args=args,
        source_channel=source_channel,
        handler_kind=resolve_handler_kind(runtime, command_name) if command_name else "unknown",
        actor_id=actor_id,
        chat_id=local_chat_id,
    )
    try:
        if not command_name:
            session.fail("empty command")
            return {"ok": False, "error": "empty command"}
        if command_name == "restart":
            session.block("human_only_restart")
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
                session.fail(f"unknown command: {command_name}")
                return {
                    "ok": False,
                    "error": f"unknown command: {command_name}",
                    "supported_commands": supported_commands(runtime),
                }
            session.handler_kind = "registry"
        else:
            session.handler_kind = "native"

        store = _CaptureStore(messages=[])
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
                session.fail(e)
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
    finally:
        session.finish()
