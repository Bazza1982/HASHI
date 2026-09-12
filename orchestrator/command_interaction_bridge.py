"""Reuse HASHI's command and callback owners through a UI-neutral projection."""
from __future__ import annotations

import asyncio
import logging
import re
from types import SimpleNamespace
from typing import Mapping

from orchestrator.command_interactions import (
    Binding, Capture, CapturedQuery, InteractionError, MenuStore, VERSION,
    ID_PATTERN, perform_action, validate_operation,
)

logger = logging.getLogger("HASHI.CommandInteractions")


def _store(runtime):
    store = getattr(runtime, "_command_interaction_store", None)
    if store is None:
        store = MenuStore()
        runtime._command_interaction_store = store
    return store


def _allowed(runtime, command):
    from orchestrator.admin_local_testing import supported_commands
    # Preserve the existing local-admin boundary; /restart is Telegram human-only.
    if command == "restart" or command not in supported_commands(runtime):
        return False
    check = getattr(runtime, "_is_command_allowed", None)
    return not callable(check) or bool(check(command))


def _resolve(runtime, data):
    from orchestrator.runtime_command_binding import CALLBACK_BINDINGS
    from orchestrator.command_registry import load_runtime_callbacks
    for binding in CALLBACK_BINDINGS:
        callback = getattr(runtime, binding.method_name, None)
        if callback and re.match(binding.pattern, data):
            base = getattr(callback, "__func__", callback)
            return (("native", binding.pattern, binding.method_name, id(base)), callback, False, binding.method_name)
    for binding in load_runtime_callbacks():
        if re.match(binding.pattern, data):
            callback = binding.callback
            return (("registry", binding.pattern, id(callback)), callback, True, None)
    return None


def _catalogue(runtime, locale):
    from orchestrator import ui_language
    from orchestrator.command_specs import COMMAND_SPECS
    from orchestrator.command_registry import runtime_command_map
    from orchestrator.runtime_command_binding import get_flexible_bot_commands
    from orchestrator.admin_local_testing import supported_commands
    specs = {s.name: s for s in COMMAND_SPECS}
    dynamic = runtime_command_map()
    available = set(supported_commands(runtime))
    # Both Telegram and this projection derive from the same owner. Dynamic
    # overrides win, just as the runtime registry does, with no second list.
    records = {}
    for cmd in get_flexible_bot_commands(runtime, locale=locale):
        name = cmd.command
        if name not in available:
            continue
        spec = None if name in dynamic else specs.get(name)
        enabled = _allowed(runtime, name)
        records[name] = {
            "name": name,
            "description": cmd.description,
            "usage": spec.guide.usage if spec and spec.guide else "/" + name,
            "enabled": enabled,
            "reason": None if enabled else "disabled_by_policy",
        }
    return {"ok": True, "command_ui_version": VERSION,
            "commands": list(records.values()), "locale": ui_language.normalize_locale(locale)}


def _refresh_signature(runtime):
    getter = getattr(runtime, "get_runtime_metadata", None)
    if not callable(getter):
        return None
    try:
        metadata = getter()
        return repr(tuple(metadata.get(key) for key in (
            "engine", "active_backend", "model", "effort", "type", "allowed_backends", "is_active")))
    except Exception:
        return None


async def dispatch_command_interaction(runtime, payload: Mapping, metadata: Mapping) -> dict:
    """Only call from authenticated Backend API or its private Worker RPC."""
    from orchestrator import ui_language
    from orchestrator.admin_local_testing import (
        _FakeUpdate, _capture_local_output, execute_local_command,
        _runtime_audit_path, _runtime_agent_name,
    )
    from orchestrator.slash_command_audit import SlashCommandAuditSession, bind_slash_command_audit_session
    try:
        validate_operation(payload)
        op = payload["op"]
        client = payload["client_id"]
        if not isinstance(metadata, Mapping):
            raise InteractionError("command_menu_binding_missing", 400)
        actor = metadata.get("actor_id")
        configured_actor = getattr(runtime.global_config, "authorized_id", None)
        if type(actor) is not int or actor != configured_actor:
            raise InteractionError("command_menu_forbidden", 403)
        checker = getattr(runtime, "_is_authorized_user", None)
        if callable(checker) and not checker(actor):
            raise InteractionError("command_menu_forbidden", 403)
        locale = ui_language.normalize_locale(payload.get("ui_locale"))
        if op == "catalogue":
            return _catalogue(runtime, locale)
        if (not isinstance(metadata.get("session_id"), str) or not metadata["session_id"]
                or not isinstance(metadata.get("connection_binding"), str) or not metadata["connection_binding"]
                or not isinstance(metadata.get("instance_id"), str)
                or type(metadata.get("context_generation")) is not int):
            raise InteractionError("command_menu_binding_missing", 400)
        binding = Binding(
            str(metadata["instance_id"]), str(runtime.name), str(actor),
            str(metadata["session_id"]), int(metadata["context_generation"]),
            client, str(metadata["connection_binding"]),
        )
        store = _store(runtime)
        command = ""
        command_line = payload.get("command", "")
        if op == "open":
            if not isinstance(command_line, str) or len(command_line) > 16384:
                raise InteractionError("command_menu_command_invalid", 400)
            from orchestrator.admin_local_testing import _split_command
            command, _ = _split_command(command_line)
            if not command_line.lstrip().startswith("/") or not _allowed(runtime, command):
                raise InteractionError("command_menu_forbidden", 403)
        capture = Capture(store, binding, command, lambda data: _resolve(runtime, data))
        capture.chat_id = actor
        before = _refresh_signature(runtime)

        async def perform():
            try:
                if op == "open":
                    for stale in store.invalidate(binding):
                        capture._record(stale)
                    result = await execute_local_command(
                        runtime, command_line, chat_id=actor, source_channel="workbench_command_ui",
                        session_metadata={**dict(metadata), "ui_locale": locale},
                        capture_store=capture,
                    )
                    if not result.get("ok"):
                        for menu in capture._owned.values():
                            menu.closed = True
                            menu.actions.clear()
                            capture._record(menu)
                        return {**capture.result(), "ok": False,
                                "error_code": "command_menu_command_failed",
                                "error": "command_menu_command_failed", "http_status": 400}
                    return capture.result(refresh_required=before != _refresh_signature(runtime))
                if op == "close":
                    menu = store.require(payload.get("menu_id"), binding, payload.get("revision"))
                    menu.revision += 1
                    menu.closed = True
                    menu.actions.clear()
                    capture._record(menu)
                    return capture.result()

                async def invoke(resolved, query: CapturedQuery):
                    _, callback, dynamic, method_name = resolved
                    update = _FakeUpdate(actor, actor, capture, "", session_metadata=metadata)
                    update.message = None
                    update.callback_query = query
                    update.effective_message = query.message
                    context = SimpleNamespace(args=[])
                    # Do not supply the Telegram network Bot as context.bot: the
                    # supported protocol is reply/edit/answer through the query.
                    lock = getattr(runtime, "_local_admin_lock", None)
                    if lock is None:
                        lock = asyncio.Lock()
                        runtime._local_admin_lock = lock
                    session = SlashCommandAuditSession(
                        audit_path=_runtime_audit_path(runtime), agent=_runtime_agent_name(runtime),
                        command_name=query.message.menu.command, args=[],
                        source_channel="workbench_command_ui_callback", handler_kind="registry" if dynamic else "native",
                        actor_id=actor, chat_id=actor,
                    )
                    async with lock:
                        original_send = getattr(runtime, "_send_text", None)
                        if original_send is not None:
                            runtime._send_text = capture.capture_send
                        try:
                            with (_capture_local_output(runtime, capture),
                                  ui_language.language_scope(runtime, update, locale=locale),
                                  bind_slash_command_audit_session(session)):
                                if dynamic:
                                    await callback(runtime, update, context)
                                else:
                                    wrap = getattr(runtime, "_wrap_callback", None)
                                    handler = wrap(method_name, callback) if callable(wrap) else callback
                                    await handler(update, context)
                        except BaseException:
                            session.fail("command_menu_callback_failed")
                            raise
                        finally:
                            if original_send is not None:
                                runtime._send_text = original_send
                            session.finish()
                result = await perform_action(store, binding, payload, capture, actor_id=actor,
                                              authorize=lambda cmd: _allowed(runtime, cmd), invoke=invoke)
                result["refresh_required"] = before != _refresh_signature(runtime)
                return result
            except InteractionError as exc:
                return {**exc.result(), "messages": capture.messages}
            except Exception as exc:
                logger.warning("Command interaction failed: %s", type(exc).__name__)
                return {**InteractionError("command_menu_outcome_unknown").result(),
                        "messages": capture.messages}
            finally:
                capture.active = False

        return await store.once(binding, payload.get("request_id"), payload, perform)
    except InteractionError as exc:
        return exc.result()
