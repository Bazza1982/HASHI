from __future__ import annotations

import asyncio
import shlex
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from orchestrator.chat_projection_transport import try_dispatch_chat_projection_transport
from orchestrator.voice_confirmation_transport import (
    try_dispatch_voice_confirmation_transport,
)
from orchestrator.command_interaction_transport import (
    try_dispatch_command_interaction_transport,
)
from orchestrator.command_registry import runtime_command_map
from orchestrator.frontend_compatibility import (
    ConnectorLocalCommand,
    normalize_compatibility_command,
)
from orchestrator.runtime_command_binding import COMMAND_BINDINGS
from orchestrator import (
    runtime_menu_views,
    slash_command_audit,
    ui_language,
    workbench_telegram_state,
)
from orchestrator.slash_command_audit import (
    SlashCommandAuditSession,
    default_audit_path,
    is_supported_slash_command,
    looks_like_slash_command,
    parse_slash_command_text,
    resolve_handler_kind,
    split_slash_command_words,
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
    active: bool = True

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


_LOCAL_OUTPUT_CAPTURE: ContextVar[Any] = ContextVar("local_command_output", default=None)


@contextmanager
def _capture_local_output(runtime, store):
    token = _LOCAL_OUTPUT_CAPTURE.set((runtime, store))
    try:
        yield
    finally:
        store.active = False
        _LOCAL_OUTPUT_CAPTURE.reset(token)


async def capture_local_command_output(runtime, chat_id, text, **kwargs) -> bool:
    capture = _LOCAL_OUTPUT_CAPTURE.get()
    if capture is None or capture[0] is not runtime or not capture[1].active:
        return False
    await capture[1].capture_send(chat_id, text, **kwargs)
    return True


class _FakeMessage:
    def __init__(self, store: _CaptureStore, text: str):
        self._store = store
        self.text = text

    async def reply_text(self, text: str, **kwargs):
        return await self._store.capture_reply(text, **kwargs)


class _FakeUpdate:
    def __init__(
        self,
        user_id: int,
        chat_id: int | str,
        store: _CaptureStore,
        text: str,
        *,
        session_metadata: Mapping[str, Any] | None = None,
    ):
        self.effective_user = SimpleNamespace(id=user_id)
        self.effective_chat = SimpleNamespace(id=chat_id)
        self.message = _FakeMessage(store, text)
        metadata = dict(session_metadata or {})
        trusted_owner = str(metadata.get("_hashi_owner_id") or "").strip() or None
        session_owner = trusted_owner or (
            str(metadata.get("owner_id") or "").strip() or None
        )
        self._hashi_session_surface = metadata.get("session_surface")
        self._hashi_session_channel_key = metadata.get("session_channel_key")
        self._hashi_owner_id = trusted_owner
        self._hashi_session_owner_id = session_owner
        self._hashi_session_id = metadata.get("session_id")
        self._hashi_session_context_generation = metadata.get(
            "context_generation"
        )
        self._hashi_ui_locale = metadata.get("ui_locale")
        # Local command projections have no Telegram update number.  Preserve
        # the typed frontend invocation identity so each command response gets
        # its own stable Session presentation idempotency key.
        self.update_id = metadata.get("frontend_invocation_id")


def _trusted_command_session_metadata(
    session_metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Project an authenticated command owner onto the Session owner field."""

    metadata = dict(session_metadata or {})
    trusted_owner = str(metadata.get("_hashi_owner_id") or "").strip()
    if trusted_owner:
        metadata["_hashi_owner_id"] = trusted_owner
        metadata["owner_id"] = trusted_owner
    return metadata


def _local_command_session_metadata(
    *,
    source_channel: str,
    chat_id: int | str | None,
    session_metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    metadata = _trusted_command_session_metadata(session_metadata)
    normalized = str(source_channel or "").strip().lower()
    if not metadata.get("session_surface"):
        if "whatsapp" in normalized:
            metadata["session_surface"] = "whatsapp"
        elif normalized.startswith(("api", "workbench", "browser")):
            metadata["session_surface"] = "workbench"
        else:
            metadata["session_surface"] = "telegram"
    if not metadata.get("session_channel_key"):
        surface = str(metadata["session_surface"])
        metadata["session_channel_key"] = (
            str(chat_id)
            if chat_id is not None and surface in {"telegram", "whatsapp"}
            else "default"
        )
    return metadata


def _split_command(command_line: str) -> tuple[str, list[str]]:
    raw = (command_line or "").strip()
    if not raw:
        return "", []
    if raw.startswith("/"):
        raw = raw[1:]
    parts = split_slash_command_words(raw)
    if not parts:
        return "", []
    return parts[0].split("@", 1)[0].lower(), parts[1:]


def _format_slash_command_line(command_name: str, args: list[str]) -> str:
    line = f"/{command_name}"
    if args:
        line += " " + " ".join(shlex.quote(arg) for arg in args)
    return line


def supported_commands(runtime) -> list[str]:
    if getattr(runtime, "is_function_worker_proxy", False):
        provider = getattr(runtime, "supported_commands", None)
        if callable(provider):
            return sorted(set(str(item) for item in provider()))
    supported = []
    for binding in COMMAND_BINDINGS:
        if hasattr(runtime, f"cmd_{binding.name}") or hasattr(
            runtime, binding.method_name
        ):
            supported.append(binding.name)
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
    session_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    voice_confirmation = await try_dispatch_voice_confirmation_transport(
        runtime, text, source_channel=source_channel,
    )
    if voice_confirmation is not None:
        return voice_confirmation
    projection = await try_dispatch_chat_projection_transport(
        runtime, text, source_channel=source_channel,
    )
    if projection is not None:
        return projection
    interaction = await try_dispatch_command_interaction_transport(
        runtime,
        text,
        source_channel=source_channel,
    )
    if interaction is not None:
        return interaction
    if not looks_like_slash_command(text):
        return None
    if getattr(runtime, "is_function_worker_proxy", False):
        return await runtime.execute_slash_command(
            text,
            source_channel=source_channel,
            chat_id=chat_id,
            session_metadata=session_metadata,
        )
    command_name, args = parse_slash_command_text(text)
    if command_name in {"telegram", "whatsapp"}:
        return await _execute_connector_mirror_command(
            runtime,
            command_name,
            args,
            chat_id=chat_id,
            source_channel=source_channel,
            session_metadata=session_metadata,
        )
    if not is_supported_slash_command(runtime, command_name):
        return None

    is_allowed = getattr(runtime, "_is_command_allowed", None)
    if callable(is_allowed) and not is_allowed(command_name):
        local_chat_id = chat_id or runtime.global_config.authorized_id
        actor_id = getattr(runtime.global_config, "authorized_id", None)
        session = SlashCommandAuditSession(
            audit_path=_runtime_audit_path(runtime),
            agent=_runtime_agent_name(runtime),
            command_name=command_name,
            args=args,
            source_channel=source_channel,
            handler_kind=resolve_handler_kind(runtime, command_name),
            actor_id=actor_id,
            chat_id=local_chat_id,
        )
        try:
            session.block("command_disabled")
            return {
                "ok": False,
                "command": command_name,
                "args": args,
                "error": f"/{command_name} is disabled for this agent.",
            }
        finally:
            session.finish()

    return await execute_local_command(
        runtime,
        _format_slash_command_line(command_name, args),
        chat_id=chat_id,
        source_channel=source_channel,
        session_metadata=session_metadata,
    )


async def _execute_connector_mirror_command(
    runtime,
    connector_id: str,
    args: list[str],
    *,
    chat_id: int | str | None = None,
    source_channel: str = "api_chat",
    session_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Handle an owner-scoped mirror switch from any authenticated surface."""

    from orchestrator import runtime_session
    from orchestrator.connector_delivery_preferences import (
        get_connector_preference, load_preferences, set_connector_preference,
    )

    session_metadata = _trusted_command_session_metadata(session_metadata)
    bridge_home = getattr(
        getattr(runtime, "global_config", None), "bridge_home", None
    )
    local_chat_id = chat_id or getattr(
        getattr(runtime, "global_config", None), "authorized_id", None
    )
    session = SlashCommandAuditSession(
        audit_path=_runtime_audit_path(runtime),
        agent=_runtime_agent_name(runtime),
        command_name=connector_id,
        args=list(args or []),
        source_channel=source_channel,
        handler_kind=resolve_handler_kind(runtime, connector_id),
        actor_id=getattr(
            getattr(runtime, "global_config", None), "authorized_id", None
        ),
        chat_id=local_chat_id,
    )
    base_result = {"command": connector_id, "args": list(args or [])}
    try:
        is_allowed = getattr(runtime, "_is_command_allowed", None)
        if callable(is_allowed) and not is_allowed(connector_id):
            session.block("command_disabled")
            return {
                **base_result,
                "ok": False,
                "error": f"/{connector_id} is disabled for this agent.",
            }
        if bridge_home is None:
            session.fail("connector delivery state unavailable")
            return {
                **base_result,
                "ok": False,
                "error": "Connector delivery state is unavailable on this instance.",
            }
        metadata = (
            dict(session_metadata) if isinstance(session_metadata, Mapping) else {}
        )
        owner_id = runtime_session.owner_id(runtime, str(metadata.get("owner_id") or "").strip() or None)

        def _mirror_card_text(mirror: bool) -> str:
            with ui_language.language_scope(runtime, actor_id=owner_id):
                if connector_id == "telegram":
                    return runtime_menu_views.telegram_menu_text(enabled=mirror)
                return runtime_menu_views.whatsapp_menu_text(enabled=mirror)

        try:
            requested = workbench_telegram_state.parse_mirror_arg(args)
        except ValueError as exc:
            session.fail(exc)
            return {
                **base_result,
                "ok": False,
                "error": f"Usage: /{connector_id} on|off",
                "usage": f"/{connector_id} on|off",
            }
        if requested is None:
            snapshot = load_preferences(bridge_home)
            mirror = get_connector_preference(
                bridge_home, owner_id, connector_id, "mirror",
                default=connector_id == "telegram",
            )
            return {
                **base_result,
                "ok": True,
                "mirror_enabled": mirror,
                f"{connector_id}_mirror": mirror,
                "owner_id": owner_id,
                "revision": int(snapshot.get("revision") or 0),
                "messages": [
                    {
                        "channel": "reply",
                        "text": _mirror_card_text(mirror),
                    }
                ],
            }
        try:
            snapshot = set_connector_preference(
                bridge_home, owner_id, connector_id, "mirror", bool(requested)
            )
        except (OSError, ValueError) as exc:
            session.fail(exc)
            return {
                **base_result,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        mirror = bool(snapshot["owners"][owner_id]["connectors"][connector_id]["mirror"])
        return {
            **base_result,
            "ok": True,
            "mirror_enabled": mirror,
            f"{connector_id}_mirror": mirror,
            "owner_id": owner_id,
            "revision": int(snapshot.get("revision") or 0),
            "messages": [
                {
                    "channel": "reply",
                    "text": _mirror_card_text(mirror),
                }
            ],
        }
    finally:
        session.finish()


async def execute_local_command(
    runtime,
    command_line: str,
    chat_id: int | str | None = None,
    source_channel: str = "workbench_api",
    session_metadata: Mapping[str, Any] | None = None,
    *,
    capture_store: Any | None = None,
) -> dict[str, Any]:
    session_metadata = _trusted_command_session_metadata(session_metadata)
    if getattr(runtime, "is_function_worker_proxy", False):
        result = await runtime.execute_slash_command(
            command_line,
            source_channel=source_channel,
            chat_id=chat_id,
            session_metadata=session_metadata,
        )
        if result is not None:
            return result
        command_name, _args = _split_command(command_line)
        return {
            "ok": False,
            "error": f"unknown command: {command_name or '(empty)'}",
            "supported_commands": supported_commands(runtime),
        }
    voice_confirmation = await try_dispatch_voice_confirmation_transport(
        runtime, command_line, source_channel=source_channel,
    )
    if voice_confirmation is not None:
        return voice_confirmation
    projection = await try_dispatch_chat_projection_transport(
        runtime, command_line, source_channel=source_channel,
    )
    if projection is not None:
        return projection
    command_name, args = _split_command(command_line)
    local_chat_id = (
        chat_id if chat_id is not None else runtime.global_config.authorized_id
    )
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
        try:
            frontend_boundary = normalize_compatibility_command(
                command_line,
                source_channel=source_channel,
                session_metadata=session_metadata,
            )
        except ConnectorLocalCommand as exc:
            session.block("connector_local_command")
            return {
                "ok": False,
                "command": command_name,
                "args": args,
                "error_code": "connector_local_command",
                "error": str(exc),
            }
        except ValueError as exc:
            session.block("frontend_adapter_rejected")
            return {
                "ok": False,
                "command": command_name,
                "args": args,
                "error_code": "frontend_adapter_rejected",
                "error": str(exc),
            }
        binding = next(
            (item for item in COMMAND_BINDINGS if item.name == command_name),
            None,
        )
        conventional_method_name = f"cmd_{command_name}"
        method_name = (
            conventional_method_name
            if hasattr(runtime, conventional_method_name)
            else binding.method_name
            if binding
            else conventional_method_name
        )
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

        store = capture_store if capture_store is not None else _CaptureStore(messages=[])
        local_session_metadata = _local_command_session_metadata(
            source_channel=source_channel,
            chat_id=chat_id,
            session_metadata=session_metadata,
        )
        if not (session_metadata or {}).get("session_surface"):
            connector = frontend_boundary["connector_id"]
            local_session_metadata["session_surface"] = {
                "backend_api": "workbench", "session_api": "workbench",
            }.get(connector, connector)
        update = _FakeUpdate(
            runtime.global_config.authorized_id,
            local_chat_id,
            store,
            command_line,
            session_metadata=local_session_metadata,
        )
        context = SimpleNamespace(
            args=args,
            source_channel=source_channel,
            frontend_operation=frontend_boundary["operation"],
            frontend_connector_id=frontend_boundary["connector_id"],
        )

        lock = getattr(runtime, "_local_admin_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            setattr(runtime, "_local_admin_lock", lock)

        async with lock:
            original_send_text = getattr(runtime, "_send_text", None)
            if original_send_text is not None:
                runtime._send_text = store.capture_send
            try:
                with (
                    _capture_local_output(runtime, store),
                    ui_language.language_scope(
                        runtime,
                        update,
                        locale=str(local_session_metadata.get("ui_locale") or "") or None,
                    ),
                    slash_command_audit.bind_slash_command_audit_session(session),
                ):
                    if registry_command is not None:
                        handler_result = await registry_command.callback(
                            runtime, update, context
                        )
                    else:
                        handler_result = await method(update, context)
            except Exception as e:
                session.fail(e)
                return {
                    "ok": False,
                    "command": command_name,
                    "args": args,
                    "messages": store.messages,
                    "error": (str(getattr(e, "error_code")) if getattr(e, "error_code", None)
                              else f"{type(e).__name__}: {e}"),
                    "error_code": getattr(e, "error_code", "command_execution_failed"),
                    "request_outcome": getattr(e, "request_outcome", "unknown"),
                }
            finally:
                if original_send_text is not None:
                    runtime._send_text = original_send_text

        response = {
            "ok": True,
            "command": command_name,
            "args": args,
            "messages": store.messages,
            **({"derived_request_id": context.derived_request_id, "request_outcome": "accepted"}
               if getattr(context, "derived_request_id", None) else {}),
        }
        if handler_result is not None:
            response["result"] = _json_safe(handler_result)
        return response
    finally:
        session.finish()
