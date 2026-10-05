"""Reuse HASHI's command and callback owners through a UI-neutral projection."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from types import SimpleNamespace
from typing import Mapping

from orchestrator.command_interactions import (
    Binding, Capture, CapturedQuery, InteractionError, MenuStore, VERSION,
    perform_action, replay_without_actions, validate_operation,
)

logger = logging.getLogger("HASHI.CommandInteractions")


def build_frontend_command_invocation(
    payload: Mapping,
    metadata: Mapping,
    *,
    actor: int,
    command_name: str,
    arguments: list[str],
    decision: str = "allowed",
    issued_action_id: str | None = None,
    revision: int | None = None,
) -> dict:
    """Build the canonical, privacy-preserving command identity before effects."""

    from orchestrator.frontend_connector_registry import (
        canonical_connector_id,
        endpoint_id_for,
        require_connector_operation,
    )
    from orchestrator.frontend_contracts import normalize_command_invocation

    client_id = str(payload["client_id"])
    request_id = str(payload["request_id"])
    ingress_transport = str(metadata.get("ingress_transport") or "command-ui")
    connector_id = canonical_connector_id(
        str(metadata.get("connector_id") or "workbench"),
        ingress_transport=ingress_transport,
        surface=str(metadata.get("session_surface") or "workbench"),
    )
    require_connector_operation(connector_id, "ingress", "command")
    idempotency_digest = hashlib.sha256(
        f"{client_id}\0{request_id}".encode("utf-8")
    ).hexdigest()
    actor_digest = hashlib.sha256(f"actor\0{actor}".encode("utf-8")).hexdigest()
    endpoint_id = endpoint_id_for(
        connector_id,
        ingress_transport=ingress_transport,
        channel_key=str(metadata["connection_binding"]),
    )
    invocation_digest = hashlib.sha256(
        f"{connector_id}\0{endpoint_id}\0{client_id}\0{request_id}".encode(
            "utf-8"
        )
    ).hexdigest()[:32]
    return normalize_command_invocation(
        {
            "type": "hashi.frontend-command",
            "version": 2,
            "invocation_id": f"cmd_{invocation_digest}",
            "request_id": request_id,
            "connector_id": connector_id,
            "endpoint_id": endpoint_id,
            "session_id": str(metadata["session_id"]),
            "context_generation": int(metadata["context_generation"]),
            "command": command_name,
            "issued_action_id": issued_action_id,
            "revision": revision,
            "arguments": arguments,
            "actor_digest": f"sha256:{actor_digest}",
            "idempotency_digest": f"sha256:{idempotency_digest}",
            "authorization": {"decision": decision, "scope": "owner"},
        }
    )


def _store(runtime):
    store = getattr(runtime, "_command_interaction_store", None)
    if store is None:
        store = MenuStore()
        runtime._command_interaction_store = store
    return store


def _allowed(runtime, command):
    from orchestrator.admin_local_testing import supported_commands
    if command not in supported_commands(runtime):
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
    from orchestrator.runtime_command_binding import get_flexible_picker_commands
    from orchestrator.admin_local_testing import supported_commands
    specs = {s.name: s for s in COMMAND_SPECS}
    dynamic = runtime_command_map()
    available = set(supported_commands(runtime))
    # Both Telegram and this projection derive from the same owner. Dynamic
    # overrides win, just as the runtime registry does, with no second list.
    records = {}
    for cmd in get_flexible_picker_commands(runtime, locale=locale):
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
        _format_slash_command_line, _runtime_audit_path, _runtime_agent_name,
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

        def command_invocation(
            command_name: str,
            arguments: list[str],
            *,
            decision: str,
            issued_action_id: str | None = None,
            revision: int | None = None,
        ) -> dict:
            return build_frontend_command_invocation(
                payload,
                metadata,
                actor=actor,
                command_name=command_name,
                arguments=arguments,
                decision=decision,
                issued_action_id=issued_action_id,
                revision=revision,
            )

        store = _store(runtime)
        command = ""
        command_args: list[str] = []
        typed_invocations: list[dict] = []
        command_line = payload.get("command", "")
        if op == "open":
            if not isinstance(command_line, str) or len(command_line) > 16384:
                raise InteractionError("command_menu_command_invalid", 400)
            from orchestrator.admin_local_testing import _split_command
            command, command_args = _split_command(command_line)
            if not command_line.lstrip().startswith("/") or not _allowed(runtime, command):
                raise InteractionError("command_menu_forbidden", 403)
            typed_invocations.append(
                command_invocation(command, command_args, decision="allowed")
            )
            command_line = _format_slash_command_line(
                typed_invocations[0]["command"],
                typed_invocations[0]["arguments"],
            )
        def persist_menu(menu):
            """Refresh the canonical presentation row; command execution never depends on it."""
            if not menu.presentation_message_id:
                return
            try:
                from orchestrator import runtime_session
                session_store = runtime_session.ensure_store(runtime)
                session_store.update_presentation_message(
                    session_id=binding.session,
                    owner_id=runtime_session.owner_id(runtime),
                    agent_id=binding.agent,
                    message_id=menu.presentation_message_id,
                    text=menu.text,
                    message_context={"command_ui": store.render(menu)},
                )
            except Exception as exc:
                logger.warning(
                    "Command menu presentation persistence failed for %s (%s)",
                    getattr(runtime, "name", "unknown"),
                    type(exc).__name__,
                )

        capture = Capture(
            store,
            binding,
            command,
            lambda data: _resolve(runtime, data),
            persist_menu=persist_menu,
        )
        capture.chat_id = actor
        before = _refresh_signature(runtime)

        async def perform():
            try:
                if op == "open":
                    result = await execute_local_command(
                        runtime,
                        command_line,
                        chat_id=actor,
                        source_channel=str(
                            metadata.get("source_channel")
                            or "workbench_command_ui"
                        ),
                        session_metadata={
                            **dict(metadata),
                            "ui_locale": locale,
                            "frontend_invocation_id": typed_invocations[0][
                                "invocation_id"
                            ],
                        },
                        capture_store=capture,
                    )
                    if not result.get("ok"):
                        for menu in capture._owned.values():
                            menu.closed = True
                            menu.actions.clear()
                            capture._record(menu)
                        code = result.get("error_code") or "command_menu_command_failed"
                        return {**capture.result(), "ok": False,
                                "error_code": code, "error": code,
                                "request_outcome": result.get("request_outcome", "unknown"),
                                "http_status": 400}
                    extra = {}
                    if "result" in result:
                        extra["result"] = result["result"]
                    response = capture.result(
                        refresh_required=before != _refresh_signature(runtime),
                        command_invocation=typed_invocations[0],
                        **extra,
                    )
                    if result.get("derived_request_id"):
                        response.update(derived_request_id=result["derived_request_id"], request_outcome="accepted")
                    return response
                if op == "close":
                    menu = store.require(payload.get("menu_id"), binding, payload.get("revision"))
                    typed_invocations.append(
                        command_invocation(
                            "command-menu.close",
                            [],
                            decision="allowed",
                            issued_action_id=str(payload.get("menu_id") or ""),
                            revision=int(payload["revision"]),
                        )
                    )
                    menu.revision += 1
                    menu.closed = True
                    menu.actions.clear()
                    capture._record(menu)
                    return capture.result(command_invocation=typed_invocations[0])

                async def invoke(resolved, query: CapturedQuery):
                    _, callback, dynamic, method_name = resolved
                    typed_invocations.append(
                        command_invocation(
                            query.message.menu.command,
                            [],
                            decision="allowed",
                            issued_action_id=str(payload["button_id"]),
                            revision=int(payload["revision"]),
                        )
                    )
                    update = _FakeUpdate(
                        actor,
                        actor,
                        capture,
                        "",
                        session_metadata={
                            **dict(metadata),
                            "ui_locale": locale,
                            "frontend_invocation_id": typed_invocations[-1][
                                "invocation_id"
                            ],
                        },
                    )
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
                                    return await callback(runtime, update, context)
                                else:
                                    wrap = getattr(runtime, "_wrap_callback", None)
                                    handler = wrap(method_name, callback) if callable(wrap) else callback
                                    return await handler(update, context)
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
                if typed_invocations:
                    result["command_invocation"] = typed_invocations[0]
                return result
            except InteractionError as exc:
                return {**exc.result(), "messages": capture.messages}
            except Exception as exc:
                logger.warning("Command interaction failed: %s", type(exc).__name__)
                return {**InteractionError("command_menu_outcome_unknown").result(),
                        "messages": capture.messages}
            finally:
                capture.active = False

        async def perform_with_durable_invocation():
            if metadata.get("_durable_command_invocation") is not True:
                return await perform()

            from orchestrator import runtime_session
            from orchestrator.session_store import IdempotencyConflict, SessionConflict

            session_id = str(metadata.get("session_id") or "").strip()
            owner = str(
                metadata.get("owner_id") or runtime_session.owner_id(runtime)
            ).strip()
            generation = metadata.get("context_generation")
            if (not session_id or not owner or type(generation) is not int
                    or generation < 1):
                raise InteractionError("command_menu_binding_missing", 400)

            if op == "open":
                durable_invocation = typed_invocations[0]
            elif op == "close":
                menu = store.require(
                    payload.get("menu_id"), binding, payload.get("revision")
                )
                durable_invocation = command_invocation(
                    "command-menu.close",
                    [],
                    decision="allowed",
                    issued_action_id=str(payload.get("menu_id") or ""),
                    revision=int(payload["revision"]),
                )
            else:
                menu = store.require(
                    payload.get("menu_id"), binding, payload.get("revision")
                )
                button_id = str(payload.get("button_id") or "")
                if button_id not in menu.actions:
                    raise InteractionError("command_menu_button_invalid", 400)
                durable_invocation = command_invocation(
                    menu.command,
                    [],
                    decision="allowed",
                    issued_action_id=button_id,
                    revision=int(payload["revision"]),
                )

            try:
                from orchestrator.frontend_command_admission import (
                    reserve_frontend_command_invocation,
                )

                ticket = reserve_frontend_command_invocation(
                    runtime,
                    session_id=session_id,
                    owner_id=owner,
                    client_id=str(payload["client_id"]),
                    request_id=str(payload["request_id"]),
                    context_generation=generation,
                    payload=payload,
                    invocation=durable_invocation,
                )
            except IdempotencyConflict as exc:
                raise InteractionError("command_menu_request_conflict", 409) from exc
            except SessionConflict as exc:
                raise InteractionError("command_menu_stale", 409) from exc

            if ticket.state == "completed":
                replay = replay_without_actions(ticket.response or {})
                replay["command_event_id"] = ticket.event_id
                return replay
            if ticket.state != "reserved":
                return {
                    **InteractionError("command_menu_outcome_unknown", 409).result(),
                    "messages": capture.messages,
                    "replayed": True,
                    "outcome_unknown": True,
                }

            result = await perform()
            if not isinstance(result, Mapping):
                raise InteractionError("command_menu_response_invalid", 502)
            response = dict(result)
            response.setdefault("command_invocation", durable_invocation)
            completion = ticket.complete(response)
            completed = dict(completion["response"])
            completed["command_event_id"] = completion.get("event_id")
            completed["replayed"] = bool(completion.get("replayed"))
            return completed

        return await store.once(
            binding,
            payload.get("request_id"),
            payload,
            perform_with_durable_invocation,
        )
    except InteractionError as exc:
        return exc.result()
