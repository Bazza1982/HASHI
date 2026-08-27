from __future__ import annotations

import asyncio
import html
import inspect
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from orchestrator.flexible_backend_registry import is_cli_backend
from orchestrator.session_store import SessionConflict, SessionNotFound, SessionStore

_INTERNAL_NON_CHAT_SOURCES = frozenset({"startup", "system", "session_reset"})
_SCHEDULED_SOURCES = frozenset(
    {
        "scheduler",
        "scheduler-retry",
        "scheduler-skill",
        "loop_skill",
        "heartbeat",
        "cron",
        "proactive",
        "background-job-event",
        "background_job_event",
    }
)


def _active_engine(runtime: Any) -> str:
    config = getattr(runtime, "config", None)
    return str(
        getattr(config, "active_backend", None)
        or getattr(config, "engine", "")
        or ""
    )


def _active_backend(runtime: Any) -> Any:
    manager = getattr(runtime, "backend_manager", None)
    return getattr(manager, "current_backend", None) if manager else getattr(runtime, "backend", None)


def _uses_cli_session_semantics(engine: str) -> bool:
    return str(engine or "").strip().lower() != "her-v2" and is_cli_backend(engine)


def ensure_store(runtime: Any) -> SessionStore:
    store = getattr(runtime, "session_store", None)
    if isinstance(store, SessionStore):
        return store
    config = runtime.global_config
    configured_root = getattr(config, "bridge_home", None) or getattr(
        config, "project_root", None
    )
    if configured_root:
        store = SessionStore.from_global_config(config)
    else:
        # Focused tests and embedded callers sometimes provide only a workspace.
        # Keep their Session state local instead of falling back to the process CWD.
        workspace = Path(getattr(runtime, "workspace_dir", "."))
        store = SessionStore(
            workspace / "state" / "sessions.sqlite3",
            instance_id=str(getattr(config, "instance_id", "HASHI") or "HASHI"),
        )
    runtime.session_store = store
    return store


def owner_id(runtime: Any, explicit: str | None = None) -> str:
    return SessionStore.owner_id_for(runtime.global_config, explicit)


def initialize_runtime_sessions(runtime: Any) -> dict[str, Any]:
    store = ensure_store(runtime)
    default = store.ensure_default_session(owner_id=owner_id(runtime), agent_id=runtime.name)
    store.set_promotion_schedule(agent_id=runtime.name)
    runtime.default_session_id = default["session_id"]
    runtime._session_memory_stores = {}
    return default


def _surface_and_channel(
    runtime: Any,
    *,
    source: str,
    chat_id: Any,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[str, str, bool]:
    del runtime
    metadata = dict(metadata or {})
    explicit_surface = str(metadata.get("session_surface") or "").strip().lower()
    explicit_channel = str(metadata.get("session_channel_key") or "").strip()
    normalized = str(source or "").strip().lower()
    scheduled = normalized in _SCHEDULED_SOURCES or normalized.startswith(
        ("scheduler:", "cron:", "heartbeat:", "proactive:")
    )
    if scheduled:
        return "scheduled", "default", True
    if explicit_surface:
        return explicit_surface, explicit_channel or "default", False
    if "whatsapp" in normalized or normalized.startswith("wa:"):
        return "whatsapp", explicit_channel or str(chat_id), False
    if normalized in {
        "text",
        "photo",
        "voice",
        "audio",
        "video",
        "document",
        "sticker",
        "multimodal",
    }:
        return "telegram", explicit_channel or str(chat_id), False
    if normalized.startswith(("bridge:", "bridge-transfer:", "bridge-fork:")):
        return "bridge", explicit_channel or "default", True
    if normalized.startswith(("api", "browser", "workbench", "session-api")):
        return "workbench", explicit_channel or "default", False
    # Telegram command-generated sources include skill:, wiki:, handoff,
    # park-load, retry-handoff, and voice_transcript. They carry the Telegram
    # chat ID even though their source is not a media handler name.
    if chat_id is not None:
        return "telegram", explicit_channel or str(chat_id), False
    return "workbench", explicit_channel or "default", False


def resolve_request_session(
    runtime: Any,
    *,
    source: str,
    chat_id: Any,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], str, str, str]:
    metadata = dict(metadata or {})
    resolved_owner = owner_id(runtime, str(metadata.get("owner_id") or "") or None)
    surface, channel_key, default_only = _surface_and_channel(
        runtime, source=source, chat_id=chat_id, metadata=metadata
    )
    session = ensure_store(runtime).resolve_session(
        owner_id=resolved_owner,
        agent_id=runtime.name,
        surface=surface,
        channel_key=channel_key,
        explicit_session_id=str(metadata.get("session_id") or "") or None,
        default_only=default_only,
    )
    return session, resolved_owner, surface, channel_key


def accept_request(
    runtime: Any,
    *,
    request_id: str,
    chat_id: Any,
    prompt: str,
    source: str,
    request_metadata: Mapping[str, Any] | None,
    request_content: Mapping[str, Any] | None,
    idempotency_key: str | None,
) -> tuple[dict[str, Any], Any | None, str, str, str]:
    session, resolved_owner, surface, channel_key = resolve_request_session(
        runtime, source=source, chat_id=chat_id, metadata=request_metadata
    )
    if str(source or "").strip().lower() in _INTERNAL_NON_CHAT_SOURCES:
        return session, None, resolved_owner, surface, channel_key
    blocks = None
    if isinstance(request_content, Mapping) and isinstance(request_content.get("content"), list):
        blocks = [
            dict(block)
            for block in request_content["content"]
            if isinstance(block, Mapping)
        ]
    accepted = ensure_store(runtime).accept_run(
        session_id=session["session_id"],
        owner_id=resolved_owner,
        agent_id=runtime.name,
        request_id=request_id,
        text=prompt,
        source=source,
        idempotency_key=str(idempotency_key or f"legacy:{request_id}"),
        execution_mode=str((request_metadata or {}).get("execution_mode") or "") or None,
        content=blocks,
        parent_run_id=str((request_metadata or {}).get("parent_run_id") or "") or None,
    )
    return session, accepted, resolved_owner, surface, channel_key


def current_session(
    runtime: Any,
    *,
    surface: str,
    channel_key: str,
    explicit_owner_id: str | None = None,
    explicit_session_id: str | None = None,
) -> dict[str, Any]:
    return ensure_store(runtime).resolve_session(
        owner_id=owner_id(runtime, explicit_owner_id),
        agent_id=runtime.name,
        surface=surface,
        channel_key=channel_key,
        explicit_session_id=explicit_session_id,
    )


def _update_session_route(runtime: Any, update: Any) -> tuple[str, str, str, str | None]:
    surface = str(
        getattr(update, "_hashi_session_surface", None) or "telegram"
    ).strip().lower()
    channel_key = str(
        getattr(update, "_hashi_session_channel_key", None) or ""
    ).strip()
    if not channel_key:
        chat = getattr(getattr(update, "effective_chat", None), "id", None)
        if chat is None:
            query = getattr(update, "callback_query", None)
            chat = getattr(getattr(query, "message", None), "chat_id", "default")
        channel_key = str(chat)
    resolved_owner = owner_id(
        runtime,
        str(getattr(update, "_hashi_session_owner_id", None) or "") or None,
    )
    explicit_session_id = (
        str(getattr(update, "_hashi_session_id", None) or "").strip() or None
    )
    return surface, channel_key, resolved_owner, explicit_session_id


def current_session_for_update(runtime: Any, update: Any) -> dict[str, Any]:
    surface, channel_key, resolved_owner, explicit_session_id = _update_session_route(
        runtime, update
    )
    return current_session(
        runtime,
        surface=surface,
        channel_key=channel_key,
        explicit_owner_id=resolved_owner,
        explicit_session_id=explicit_session_id,
    )


def request_route_for_update(
    runtime: Any, update: Any
) -> tuple[Any, dict[str, Any], bool]:
    """Return queue routing that preserves the command's Session and channel."""
    surface, channel_key, resolved_owner, explicit_session_id = _update_session_route(
        runtime, update
    )
    session = current_session(
        runtime,
        surface=surface,
        channel_key=channel_key,
        explicit_owner_id=resolved_owner,
        explicit_session_id=explicit_session_id,
    )
    raw_chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    deliver_to_telegram = surface == "telegram"
    enqueue_chat_id = raw_chat_id if deliver_to_telegram else 0
    return (
        enqueue_chat_id,
        {
            "session_id": session["session_id"],
            "owner_id": resolved_owner,
            "session_surface": surface,
            "session_channel_key": channel_key,
        },
        deliver_to_telegram,
    )


def item_session_workspace(runtime: Any, item: Any) -> Path:
    session_id = str(getattr(item, "session_id", "") or "")
    generation = int(getattr(item, "context_generation", 0) or 0)
    if not session_id:
        # Legacy maintenance tools and focused unit tests do not pass through
        # the queue acceptance boundary. Keep their existing Agent workspace
        # behavior; every live chat request receives a Session ID at enqueue.
        return Path(getattr(runtime, "workspace_dir", "."))
    return ensure_store(runtime).session_workspace(session_id, generation)


def current_session_workspace(runtime: Any, update: Any | None = None) -> Path:
    if update is not None:
        session = current_session_for_update(runtime, update)
    else:
        session_id = str(getattr(runtime, "default_session_id", "") or "")
        if not session_id:
            session_id = initialize_runtime_sessions(runtime)["session_id"]
        session = ensure_store(runtime).get_session(session_id)
    return ensure_store(runtime).session_workspace(
        session["session_id"], int(session["context_generation"])
    )


def recent_exchanges(
    runtime: Any, item: Any, *, limit: int = 8
) -> list[dict[str, Any]] | None:
    session_id = str(getattr(item, "session_id", "") or "")
    if not session_id:
        return None
    return ensure_store(runtime).recent_exchanges(
        session_id,
        context_generation=int(getattr(item, "context_generation", 0) or 0) or None,
        limit=limit,
    )


def bridge_recent_exchanges(
    runtime: Any, update: Any, *, limit: int = 10
) -> list[dict[str, Any]]:
    """Return Bridge-owned recent history without a Session boundary.

    Ordinary context is Session-local. An explicit handoff is different: it
    restores the user's recent Agent timeline into whichever Session/backend
    is active now, including history retained in archived Sessions.
    """

    from orchestrator.handoff_builder import HandoffBuilder

    _surface, _channel_key, resolved_owner, _explicit_session_id = (
        _update_session_route(runtime, update)
    )
    return ensure_store(runtime).recent_agent_exchanges(
        owner_id=resolved_owner,
        agent_id=runtime.name,
        limit=limit,
        excluded_sources=HandoffBuilder.EXCLUDED_RECENT_SOURCES,
    )


def session_memory_store(runtime: Any, item: Any) -> Any:
    from orchestrator.bridge_memory import BridgeMemoryStore

    if not getattr(item, "session_id", None):
        legacy = getattr(runtime, "memory_store", None)
        if legacy is not None:
            return legacy
    workspace = item_session_workspace(runtime, item)
    key = str(workspace.resolve())
    stores = getattr(runtime, "_session_memory_stores", None)
    if not isinstance(stores, dict):
        stores = {}
        runtime._session_memory_stores = stores
    store = stores.get(key)
    if store is None:
        store = BridgeMemoryStore(workspace)
        stores[key] = store
    return store


def session_handoff_builder(
    runtime: Any,
    *,
    item: Any | None = None,
    update: Any | None = None,
    surface: str = "telegram",
    channel_key: str = "default",
) -> Any:
    from orchestrator.handoff_builder import HandoffBuilder

    if item is not None:
        if not getattr(item, "session_id", None):
            return runtime.handoff_builder
        workspace = item_session_workspace(runtime, item)
    elif update is not None:
        workspace = current_session_workspace(runtime, update)
    elif getattr(runtime, "session_store", None) is not None and getattr(
        runtime, "name", None
    ):
        session = current_session(
            runtime, surface=surface, channel_key=str(channel_key)
        )
        workspace = ensure_store(runtime).session_workspace(
            session["session_id"], int(session["context_generation"])
        )
    else:
        return runtime.handoff_builder
    key = str(workspace.resolve())
    builders = getattr(runtime, "_session_handoff_builders", None)
    if not isinstance(builders, dict):
        builders = {}
        runtime._session_handoff_builders = builders
    builder = builders.get(key)
    if builder is None:
        builder = HandoffBuilder(
            workspace,
            canonical_audit=getattr(runtime, "canonical_audit", None),
        )
        builders[key] = builder
    return builder


def record_working_exchange(
    runtime: Any,
    item: Any,
    *,
    user_text: str,
    assistant_text: str,
    assistant_source: str,
) -> None:
    if not getattr(item, "run_id", None):
        return
    store = session_memory_store(runtime, item)
    user_turn_id = store.record_turn("user", item.source, user_text)
    assistant_turn_id = store.record_turn("assistant", assistant_source, assistant_text)
    store.record_completed_exchange(
        user_text,
        assistant_text,
        item.source,
        assistant_source=assistant_source,
        user_turn_id=user_turn_id,
        assistant_turn_id=assistant_turn_id,
        user_ts=str(getattr(item, "created_at", "") or ""),
        origin="session",
        origin_ref=f"session:{item.session_id}:run:{item.run_id}",
    )


def runtime_busy(runtime: Any) -> bool:
    checker = getattr(runtime, "_backend_busy", None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except AttributeError:
        # Partially constructed runtimes are used by command-level tests and
        # recovery utilities before queue state exists.
        return False


def mark_running(runtime: Any, item: Any) -> None:
    if getattr(item, "run_id", None):
        ensure_store(runtime).mark_request_running(
            item.request_id,
            worker_id=f"{getattr(runtime.global_config, 'instance_id', 'HASHI')}:{runtime.name}",
        )


def finish_request_from_listener(runtime: Any, request_id: str, payload: Mapping[str, Any]) -> None:
    store = getattr(runtime, "session_store", None)
    if not isinstance(store, SessionStore):
        return
    success = bool(payload.get("success"))
    store.finish_request(
        request_id,
        success=success,
        assistant_text=str(payload.get("text") or "") or None,
        assistant_source=_active_engine(runtime) or runtime.name,
        error_text=str(payload.get("error") or "") or None,
    )
    capture_backend_binding(runtime, request_id=request_id)


def activate_backend_binding(runtime: Any, item: Any) -> None:
    backend = _active_backend(runtime)
    if backend is None or not getattr(item, "session_id", None):
        return
    if not bool(getattr(getattr(backend, "capabilities", None), "supports_sessions", False)):
        return
    binding = ensure_store(runtime).backend_binding(
        agent_id=runtime.name,
        session_id=item.session_id,
        context_generation=int(item.context_generation),
        backend_id=_active_engine(runtime),
    )
    if hasattr(backend, "_session_id"):
        backend._session_id = binding


def capture_backend_binding(runtime: Any, *, request_id: str) -> None:
    backend = _active_backend(runtime)
    registry = getattr(runtime, "_request_meta_by_id", None)
    metadata = registry.get(request_id, {}) if isinstance(registry, dict) else {}
    session_id = str(metadata.get("hashi_session_id") or "")
    generation = int(metadata.get("context_generation") or 0)
    if backend is None or not session_id or generation <= 0:
        return
    if not bool(getattr(getattr(backend, "capabilities", None), "supports_sessions", False)):
        return
    ensure_store(runtime).save_backend_binding(
        agent_id=runtime.name,
        session_id=session_id,
        context_generation=generation,
        backend_id=_active_engine(runtime),
        backend_thread_id=str(getattr(backend, "_session_id", "") or "") or None,
    )


def _session_workzone_path(session: Mapping[str, Any]) -> Path | None:
    value = str(session.get("workzone") or "").strip()
    return Path(value) if value else None


def session_workzone(runtime: Any, item: Any | None = None, *, update: Any | None = None) -> Path | None:
    try:
        if item is not None and getattr(item, "session_id", None):
            session = ensure_store(runtime).get_session(item.session_id)
        elif update is not None:
            session = current_session_for_update(runtime, update)
        else:
            session = ensure_store(runtime).get_session(runtime.default_session_id)
    except (AttributeError, SessionNotFound):
        return None
    return _session_workzone_path(session)


def apply_item_workzone(runtime: Any, item: Any) -> None:
    runtime._workzone_dir = session_workzone(runtime, item)
    sync = getattr(runtime, "_sync_workzone_to_backend_config", None)
    if callable(sync):
        sync()


def _prepare_clean_context(
    runtime: Any,
    *,
    disable_saved_memory: bool,
    clear_session_primer: bool = False,
) -> None:
    del disable_saved_memory
    clear_transfer_state = getattr(runtime, "_clear_transfer_state", None)
    if callable(clear_transfer_state):
        clear_transfer_state()
    runtime._pending_auto_recall_context = None
    runtime._pending_auto_recall_session_id = None
    if clear_session_primer:
        runtime._pending_session_primer = None
        runtime._pending_session_primer_session_id = None
    assembler = getattr(runtime, "context_assembler", None)
    if assembler is not None:
        assembler.turns_injection_enabled = True
        assembler.saved_memory_injection_enabled = True


async def _reset_cli_backend(runtime: Any, *, reason: str) -> str:
    del reason
    backend = _active_backend(runtime)
    if backend is None:
        return "unavailable"
    supports_sessions = bool(
        getattr(getattr(backend, "capabilities", None), "supports_sessions", False)
    )
    if supports_sessions and hasattr(backend, "handle_new_session"):
        result = backend.handle_new_session()
        if inspect.isawaitable(result):
            result = await result
        if result is False:
            raise RuntimeError("backend refused to start a new session")
        return "session"
    return "stateless"


async def reset_for_retry(runtime: Any) -> str:
    engine = _active_engine(runtime)
    _prepare_clean_context(runtime, disable_saved_memory=False, clear_session_primer=True)
    if _uses_cli_session_semantics(engine):
        await _reset_cli_backend(runtime, reason="cmd_retry_cli_reset")
        return "new"
    return "fresh"


async def _bind_session(runtime: Any, update: Any, session_id: str) -> None:
    surface, channel_key, resolved_owner, _explicit_session_id = _update_session_route(
        runtime, update
    )
    ensure_store(runtime).bind_channel(
        owner_id=resolved_owner,
        agent_id=runtime.name,
        surface=surface,
        channel_key=channel_key,
        session_id=session_id,
    )
    # Telegram Update objects are slotted and cannot carry arbitrary HASHI
    # attributes. The durable channel binding is canonical; callers already
    # hold the selected Session for any remaining work in the same command.


async def cmd_new(runtime: Any, update: Any, context: Any) -> None:
    del context
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if runtime_busy(runtime):
        await runtime._reply_text(update, "Session change is blocked while a request is running or queued.")
        return
    _surface, _channel_key, resolved_owner, _explicit_session_id = (
        _update_session_route(runtime, update)
    )
    session = ensure_store(runtime).create_session(
        owner_id=resolved_owner, agent_id=runtime.name, title="New session"
    )
    previous_workzone = getattr(runtime, "_workzone_dir", None)
    sync = getattr(runtime, "_sync_workzone_to_backend_config", None)
    logger = getattr(runtime, "logger", None)
    try:
        await _reset_cli_backend(runtime, reason="cmd_new_session")
        _prepare_clean_context(
            runtime, disable_saved_memory=False, clear_session_primer=True
        )
        runtime._workzone_dir = None
        if callable(sync):
            sync()
        await _bind_session(runtime, update, session["session_id"])
    except Exception:  # noqa: BLE001 - backend adapters expose heterogeneous failures
        if logger is not None:
            logger.exception("Could not activate new Session safely")
        runtime._workzone_dir = previous_workzone
        if callable(sync):
            try:
                sync()
            except Exception:  # noqa: BLE001 - best-effort runtime state restoration
                if logger is not None:
                    logger.warning(
                        "Could not restore the previous Workzone after Session failure",
                        exc_info=True,
                    )
        try:
            ensure_store(runtime).archive_session(
                session["session_id"], deleted=True
            )
        except Exception:  # noqa: BLE001 - best-effort cleanup after activation failure
            if logger is not None:
                logger.warning(
                    "Could not archive the inactive Session after activation failure",
                    exc_info=True,
                )
        await runtime._reply_text(
            update,
            "Could not start a new Session. The previous channel binding remains active.",
        )
        return
    await runtime._reply_text(
        update,
        f"New Session active: <code>{html.escape(session['session_id'])}</code>",
        parse_mode="HTML",
    )


async def cmd_fresh(runtime: Any, update: Any, context: Any) -> None:
    del context
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if runtime_busy(runtime):
        await runtime._reply_text(update, "Fresh context is blocked while a request is running or queued.")
        return
    session = current_session_for_update(runtime, update)
    updated = ensure_store(runtime).start_fresh_generation(
        session["session_id"], reason="user_fresh"
    )
    _prepare_clean_context(runtime, disable_saved_memory=False, clear_session_primer=True)
    try:
        from orchestrator.context_compaction import cancel_runtime_compaction

        await cancel_runtime_compaction(runtime)
    except Exception as exc:
        logger = getattr(runtime, "logger", None)
        if logger is not None:
            logger.warning("Could not cancel old Session compaction on /fresh: %s", exc)
    await _reset_cli_backend(runtime, reason="cmd_fresh_context_generation")
    await runtime._reply_text(
        update,
        f"Fresh context generation {updated['context_generation']} started. Session records were retained.",
    )


async def cmd_sessions(runtime: Any, update: Any, context: Any) -> None:
    del context
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    current = current_session_for_update(runtime, update)
    _surface, _channel_key, resolved_owner, _explicit_session_id = (
        _update_session_route(runtime, update)
    )
    rows = ensure_store(runtime).list_sessions(
        owner_id=resolved_owner, agent_id=runtime.name, include_archived=True
    )
    lines = ["<b>Sessions</b>"]
    for index, row in enumerate(rows, start=1):
        marker = "*" if row["session_id"] == current["session_id"] else " "
        default = " default" if row["is_default"] else ""
        lines.append(
            f"{marker} {index}. <code>{html.escape(row['session_id'])}</code> "
            f"{html.escape(row['title'])} [{html.escape(row['status'])}{default}]"
        )
    await runtime._reply_text(update, "\n".join(lines), parse_mode="HTML")


def _select_session(
    runtime: Any, token: str, *, explicit_owner_id: str | None = None
) -> dict[str, Any]:
    rows = ensure_store(runtime).list_sessions(
        owner_id=owner_id(runtime, explicit_owner_id),
        agent_id=runtime.name,
        include_archived=False,
    )
    clean = str(token or "").strip()
    if clean.isdigit() and 1 <= int(clean) <= len(rows):
        return rows[int(clean) - 1]
    matches = [
        row
        for row in rows
        if row["session_id"] == clean
        or row["session_id"].startswith(clean)
        or row["title"].lower() == clean.lower()
    ]
    if len(matches) != 1:
        raise SessionNotFound(clean)
    return matches[0]


async def cmd_use(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    args = list(getattr(context, "args", None) or [])
    if not args:
        await runtime._reply_text(update, "Usage: /use <Session number or ID>")
        return
    if runtime_busy(runtime):
        await runtime._reply_text(update, "Session change is blocked while a request is running or queued.")
        return
    try:
        _surface, _channel_key, resolved_owner, _explicit_session_id = (
            _update_session_route(runtime, update)
        )
        session = _select_session(
            runtime,
            " ".join(str(arg) for arg in args),
            explicit_owner_id=resolved_owner,
        )
    except SessionNotFound:
        await runtime._reply_text(update, "Session not found or selection is ambiguous.")
        return
    await _bind_session(runtime, update, session["session_id"])
    _prepare_clean_context(runtime, disable_saved_memory=False, clear_session_primer=True)
    await _reset_cli_backend(runtime, reason="cmd_use_session")
    runtime._workzone_dir = _session_workzone_path(session)
    sync = getattr(runtime, "_sync_workzone_to_backend_config", None)
    if callable(sync):
        sync()
    await runtime._reply_text(
        update,
        f"Using Session <code>{html.escape(session['session_id'])}</code>: {html.escape(session['title'])}",
        parse_mode="HTML",
    )


async def cmd_current(runtime: Any, update: Any, context: Any) -> None:
    del context
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    session = current_session_for_update(runtime, update)
    await runtime._reply_text(
        update,
        (
            f"Current Session: <code>{html.escape(session['session_id'])}</code>\n"
            f"Title: {html.escape(session['title'])}\n"
            f"Context generation: {session['context_generation']}"
        ),
        parse_mode="HTML",
    )


async def cmd_archive(runtime: Any, update: Any, context: Any) -> None:
    del context
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    session = current_session_for_update(runtime, update)
    try:
        ensure_store(runtime).archive_session(session["session_id"])
    except SessionConflict as exc:
        await runtime._reply_text(update, str(exc))
        return
    default = ensure_store(runtime).ensure_default_session(
        owner_id=_update_session_route(runtime, update)[2], agent_id=runtime.name
    )
    await _bind_session(runtime, update, default["session_id"])
    _prepare_clean_context(
        runtime, disable_saved_memory=False, clear_session_primer=True
    )
    await _reset_cli_backend(runtime, reason="cmd_archive_session")
    runtime._workzone_dir = _session_workzone_path(default)
    sync = getattr(runtime, "_sync_workzone_to_backend_config", None)
    if callable(sync):
        sync()
    await runtime._reply_text(
        update,
        "Session archived. Its messages, logs, and promoted memories were retained; the default Session is active.",
    )


def _utc_now_for_promotion() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _promoted_memory_exists(runtime: Any, origin_ref: str) -> bool:
    checker = getattr(runtime.memory_store, "memory_origin_exists", None)
    return bool(checker(origin_ref)) if callable(checker) else False


def promote_sessions(
    runtime: Any,
    *,
    session_ids: list[str] | None = None,
    trigger: str = "manual",
) -> dict[str, Any]:
    store = ensure_store(runtime)
    candidates = store.promotion_candidates(
        agent_id=runtime.name, session_ids=session_ids, limit=5000
    )
    promoted = 0
    for candidate in candidates:
        episode = f"User: {candidate['user_text']}\nAssistant: {candidate['assistant_text']}"
        recorder = runtime.memory_store.record_memory
        try:
            memory_id = recorder(
                "episodic",
                f"session:{candidate['session_id']}",
                episode,
                importance=1.0,
                origin="session_promotion",
                origin_ref=candidate["memory_origin_ref"],
                session_id=candidate["session_id"],
                run_id=candidate["run_id"],
                message_id=candidate["assistant_message_id"],
                promoted_at=_utc_now_for_promotion(),
            )
        except TypeError:
            memory_id = recorder(
                "episodic", f"session:{candidate['session_id']}", episode, importance=1.0
            )
        if memory_id is not None or _promoted_memory_exists(runtime, candidate["memory_origin_ref"]):
            if store.record_promoted(agent_id=runtime.name, candidate=candidate):
                promoted += 1
    return {
        "trigger": trigger,
        "candidate_count": len(candidates),
        "promoted_count": promoted,
        "pending_count": store.promotion_status(agent_id=runtime.name)["pending_count"],
    }


async def cmd_promote(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    args = [str(arg).strip() for arg in (getattr(context, "args", None) or [])]
    action = args[0].lower() if args else "status"
    store = ensure_store(runtime)
    if action == "status":
        status = store.promotion_status(agent_id=runtime.name)
        schedule = status["schedule"]
        await runtime._reply_text(
            update,
            (
                f"Promotion: {'on' if schedule.get('enabled') else 'off'} at "
                f"{schedule.get('local_time')} ({schedule.get('timezone')}); "
                f"{status['pending_count']} pending, {status['promoted_count']} promoted."
            ),
        )
        return
    if action in {"now", "manual"} and len(args) == 1:
        session = current_session_for_update(runtime, update)
        result = promote_sessions(
            runtime, session_ids=[session["session_id"]], trigger="manual"
        )
        await runtime._reply_text(
            update,
            f"Promoted {result['promoted_count']} completed exchange(s); {result['pending_count']} remain.",
        )
        return
    if action == "all" and len(args) >= 2 and args[1].lower() == "now":
        result = promote_sessions(runtime, trigger="manual_all")
        await runtime._reply_text(
            update,
            f"Promoted {result['promoted_count']} completed exchange(s) across all Sessions.",
        )
        return
    if action == "auto" and len(args) >= 2 and args[1].lower() in {"on", "off"}:
        schedule = store.set_promotion_schedule(
            agent_id=runtime.name, enabled=args[1].lower() == "on"
        )
        await runtime._reply_text(
            update, f"Automatic promotion {'enabled' if schedule['enabled'] else 'disabled'}."
        )
        return
    if action in {"time", "manual"} and len(args) >= 2:
        schedule = store.set_promotion_schedule(
            agent_id=runtime.name,
            local_time=args[1],
            timezone_name=args[2] if len(args) >= 3 else None,
            enabled=True,
        )
        await runtime._reply_text(
            update,
            f"Automatic promotion set to {schedule['local_time']} ({schedule['timezone']}).",
        )
        return
    await runtime._reply_text(
        update,
        "Usage: /promote status|now|all now|auto on|auto off|time HH:MM [timezone]",
    )


def _schedule_now(schedule: Mapping[str, Any]) -> tuple[datetime, str]:
    timezone_name = str(schedule.get("timezone") or "local")
    if timezone_name == "local":
        now = datetime.now().astimezone()
    else:
        try:
            now = datetime.now(ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError:
            now = datetime.now().astimezone()
    return now, now.date().isoformat()


def promotion_is_due(runtime: Any) -> tuple[bool, str]:
    schedule = ensure_store(runtime).promotion_status(agent_id=runtime.name)["schedule"]
    if not bool(schedule.get("enabled", 1)):
        return False, ""
    now, local_date = _schedule_now(schedule)
    due = (
        now.strftime("%H:%M") >= str(schedule.get("local_time") or "00:00")
        and str(schedule.get("last_local_date") or "") != local_date
    )
    return due, local_date


async def automatic_promotion_loop(runtime: Any) -> None:
    while not bool(getattr(runtime, "is_shutting_down", False)):
        try:
            due, local_date = promotion_is_due(runtime)
            if due:
                result = promote_sessions(runtime, trigger="scheduled")
                ensure_store(runtime).mark_promotion_schedule_ran(
                    agent_id=runtime.name, local_date=local_date
                )
                runtime.logger.info(
                    "Scheduled Session promotion completed: promoted=%s pending=%s",
                    result["promoted_count"],
                    result["pending_count"],
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime.logger.exception("Scheduled Session promotion failed safely: %s", exc)
        await asyncio.sleep(30.0)


def start_automatic_promotion(runtime: Any) -> None:
    task = getattr(runtime, "_session_promotion_task", None)
    if isinstance(task, asyncio.Task) and not task.done():
        return
    runtime._session_promotion_task = asyncio.create_task(
        automatic_promotion_loop(runtime), name=f"session-promotion-{runtime.name}"
    )


__all__ = [
    "accept_request",
    "activate_backend_binding",
    "apply_item_workzone",
    "bridge_recent_exchanges",
    "capture_backend_binding",
    "cmd_archive",
    "cmd_current",
    "cmd_fresh",
    "cmd_new",
    "cmd_promote",
    "cmd_sessions",
    "cmd_use",
    "current_session_for_update",
    "current_session_workspace",
    "ensure_store",
    "finish_request_from_listener",
    "initialize_runtime_sessions",
    "item_session_workspace",
    "mark_running",
    "owner_id",
    "promote_sessions",
    "promotion_is_due",
    "recent_exchanges",
    "record_working_exchange",
    "request_route_for_update",
    "reset_for_retry",
    "session_memory_store",
    "session_workzone",
    "start_automatic_promotion",
]
