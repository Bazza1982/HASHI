from __future__ import annotations

import html
import time
from pathlib import Path
from typing import Any, Mapping

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from orchestrator import ui_language
from orchestrator import workzone as workzone_module
from orchestrator.command_ui import card_title, confirm_card, selected_label, status_label
from orchestrator.session_store import SessionConflict

_PATH_REPLY_TTL_SECONDS = 300.0


# These forwarding functions deliberately resolve the provider lazily.  On the
# first hot reboot from the legacy single-Workzone build, this consumer can be
# reloaded before ``orchestrator.workzone``; the shared module object receives
# the new helpers later in the same reload pass, before any command is handled.
def access_roots_for_workzones(*args: Any, **kwargs: Any):
    return workzone_module.access_roots_for_workzones(*args, **kwargs)


def active_workzone_slots(*args: Any, **kwargs: Any):
    return workzone_module.active_workzone_slots(*args, **kwargs)


def build_workzone_prompt(*args: Any, **kwargs: Any):
    return workzone_module.build_workzone_prompt(*args, **kwargs)


def configured_workzone_slot(*args: Any, **kwargs: Any):
    return workzone_module.configured_workzone_slot(*args, **kwargs)


def display_workzone_path(*args: Any, **kwargs: Any):
    return workzone_module.display_workzone_path(*args, **kwargs)


def normalize_workzone_slot(*args: Any, **kwargs: Any):
    return workzone_module.normalize_workzone_slot(*args, **kwargs)


def normalize_workzone_state(*args: Any, **kwargs: Any):
    return workzone_module.normalize_workzone_state(*args, **kwargs)


def primary_workzone_path(*args: Any, **kwargs: Any):
    return workzone_module.primary_workzone_path(*args, **kwargs)


def resolve_workzone_input(*args: Any, **kwargs: Any):
    return workzone_module.resolve_workzone_input(*args, **kwargs)


def _workzone_slot_ids() -> tuple[str, ...]:
    return tuple(workzone_module.WORKZONE_SLOT_IDS)


def _empty_state(session_id: str = "") -> dict[str, Any]:
    return {"session_id": str(session_id), "revision": 0, "slots": []}


def _legacy_runtime_state(runtime: Any) -> dict[str, Any]:
    zone = getattr(runtime, "_workzone_dir", None)
    if zone is None:
        return _empty_state()
    return normalize_workzone_state(
        {
            "slots": [
                {"slot_id": "main", "path": str(zone), "enabled": True}
            ]
        }
    )


def install_runtime_state(runtime: Any, state: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized = normalize_workzone_state(state)
    runtime._workzone_state = normalized
    runtime._workzone_dir = primary_workzone_path(normalized)
    runtime._workzone_dirs = tuple(
        Path(item["path"])
        for item in active_workzone_slots(normalized, available_only=True)
    )
    return normalized


def session_state(runtime: Any, session_id: str) -> dict[str, Any]:
    from orchestrator import runtime_session

    store = runtime_session.ensure_store(runtime)
    getter = getattr(store, "get_workzone_set", None)
    if callable(getter):
        return normalize_workzone_state(getter(str(session_id)))
    session = store.get_session(str(session_id))
    value = str(session.get("workzone") or "").strip()
    if not value:
        return _empty_state(str(session_id))
    return normalize_workzone_state(
        {
            "session_id": str(session_id),
            "slots": [{"slot_id": "main", "path": value, "enabled": True}],
        }
    )


def sync_workzone_to_backend_config(runtime: Any) -> None:
    state = normalize_workzone_state(
        getattr(runtime, "_workzone_state", None) or _legacy_runtime_state(runtime)
    )
    install_runtime_state(runtime, state)
    primary = primary_workzone_path(state)
    available_paths = [
        str(Path(item["path"]))
        for item in active_workzone_slots(state, available_only=True)
    ]

    if runtime.config.extra is None:
        runtime.config.extra = {}
    runtime.config.extra["workzone_state"] = state
    runtime.config.extra["workzone_dirs"] = available_paths
    runtime.config.extra["workzone_revision"] = int(state["revision"])
    if primary is not None:
        runtime.config.extra["workzone_dir"] = str(primary)
    else:
        runtime.config.extra.pop("workzone_dir", None)

    backend = getattr(getattr(runtime, "backend_manager", None), "current_backend", None)
    if backend is None or getattr(backend, "config", None) is None:
        return
    if backend.config.extra is None:
        backend.config.extra = {}
    backend.config.extra["workzone_state"] = state
    backend.config.extra["workzone_dirs"] = available_paths
    backend.config.extra["workzone_revision"] = int(state["revision"])
    if primary is not None:
        backend.config.extra["workzone_dir"] = str(primary)
    else:
        backend.config.extra.pop("workzone_dir", None)

    registry = getattr(backend, "tool_registry", None)
    if registry is None:
        return
    registry.workspace_dir = primary or runtime.workspace_dir
    roots = access_roots_for_workzones(
        backend.config.resolve_access_root(),
        state,
        workspace_dir=runtime.workspace_dir,
    )
    registry.access_roots = tuple(roots)
    registry.access_root = roots[0]


def workzone_prompt_section(runtime: Any) -> list[tuple]:
    runtime._sync_workzone_to_backend_config()
    backend = getattr(runtime.backend_manager, "current_backend", None)
    can_access_files = bool(
        backend
        and (
            getattr(getattr(backend, "capabilities", None), "supports_files", False)
            or getattr(backend, "tool_registry", None) is not None
        )
    )
    state = normalize_workzone_state(getattr(runtime, "_workzone_state", None))
    section = build_workzone_prompt(
        state,
        runtime.workspace_dir,
        can_access_files=can_access_files,
    )
    if not section:
        return []
    active_metadata = [
        {
            "slot": item["slot_id"],
            "role": "primary" if item["slot_id"] == "main" else "attached",
            "path": item["path"],
            "label": item.get("label") or Path(item["path"]).name,
            "available": bool(item["available"]),
        }
        for item in active_workzone_slots(state)
    ]
    return [
        (
            section[0],
            section[1],
            {
                "key": "working_environment.workzones",
                "protected": True,
                "schema_version": 2,
                "scope": "session",
                "workzone_revision": int(state["revision"]),
                "primary_slot": "main" if primary_workzone_path(state) else None,
                "active_slots": active_metadata,
            },
        )
    ]


def _slot_display_name(item: Mapping[str, Any]) -> str:
    label = str(item.get("label") or "").strip()
    return label or Path(str(item.get("path") or "")).name or str(item.get("path") or "")


def _slot_marker(item: Mapping[str, Any] | None, slot_id: str) -> str:
    if item is None:
        return "·"
    if item.get("enabled") and not item.get("available"):
        return "⚠"
    if item.get("enabled"):
        return "★" if slot_id == "main" else "●"
    return "○"


def workzone_overview_text(
    runtime: Any,
    state: Mapping[str, Any],
    *,
    notice: str | None = None,
) -> str:
    normalized = normalize_workzone_state(state)
    configured = {item["slot_id"]: item for item in normalized["slots"]}
    active = active_workzone_slots(normalized)
    available = [item for item in active if item["available"]]
    lines = [card_title("📁", "Workzones"), ""]
    if notice:
        lines.extend([f"✅ {html.escape(str(notice))}", ""])
    lines.extend(
        [
            f"<b>{html.escape(ui_language.tr('common.current'))}</b> · "
            f"<code>{len(active)}/{len(_workzone_slot_ids())}</code> "
            f"{html.escape(ui_language.tr('workzone.active_suffix'))}",
            f"<b>{html.escape(ui_language.tr('workzone.accessible'))}</b> · "
            f"<code>{len(available)}/{len(active)}</code>",
            f"<b>{html.escape(ui_language.tr('workzone.agent_home'))}</b> · "
            f"<code>{html.escape(display_workzone_path(runtime.workspace_dir))}</code>",
            "",
            f"<b>{html.escape(ui_language.tr('workzone.slots'))}</b>",
        ]
    )
    for slot_id in _workzone_slot_ids():
        item = configured.get(slot_id)
        marker = _slot_marker(item, slot_id)
        if item is None:
            detail = ui_language.tr("common.empty")
        else:
            detail = _slot_display_name(item)
            if not item["enabled"]:
                detail += f" · {ui_language.tr('common.off')}"
            elif not item["available"]:
                detail += f" · {ui_language.tr('workzone.unavailable')}"
        role = ui_language.tr("workzone.main") if slot_id == "main" else slot_id
        lines.append(
            f"{marker} <code>{html.escape(role)}</code> · {html.escape(str(detail))}"
        )
    lines.extend(
        [
            "",
            f"<b>{html.escape(ui_language.tr('common.use'))}</b>",
            f"<code>/workzone &lt;{html.escape(ui_language.tr('workzone.path_word'))}&gt;</code> · "
            f"{html.escape(ui_language.tr('workzone.action_main'))}",
            f"<code>/workzone 1 &lt;{html.escape(ui_language.tr('workzone.path_word'))}&gt;</code> · "
            f"{html.escape(ui_language.tr('workzone.action_attached'))}",
            f"<code>/workzone all off</code> · {html.escape(ui_language.tr('workzone.action_all_off'))}",
        ]
    )
    return "\n".join(lines)


def workzone_slot_text(
    runtime: Any,
    state: Mapping[str, Any],
    slot_id: str,
    *,
    notice: str | None = None,
) -> str:
    slot = normalize_workzone_slot(slot_id)
    item = configured_workzone_slot(state, slot)
    lines = [card_title("📁", ui_language.tr("workzone.slot_title", slot=slot)), ""]
    if notice:
        lines.extend([f"✅ {html.escape(str(notice))}", ""])
    if item is None:
        lines.extend(
            [
                f"<b>{html.escape(ui_language.tr('common.current'))}</b> · "
                f"<b>{html.escape(ui_language.tr('common.empty'))}</b>",
                f"<b>{html.escape(ui_language.tr('common.slot'))}</b> · <code>{html.escape(slot)}</code>",
                "",
                html.escape(ui_language.tr("workzone.empty_effect")),
            ]
        )
        return "\n".join(lines)
    role = ui_language.tr("workzone.role_primary" if slot == "main" else "workzone.role_attached")
    state_label = status_label(bool(item["enabled"]))
    if item["enabled"] and not item["available"]:
        state_label = f"⚠ {ui_language.tr('workzone.unavailable')}"
    lines.extend(
        [
            f"<b>{html.escape(ui_language.tr('common.current'))}</b> · <b>{html.escape(state_label)}</b>",
            f"<b>{html.escape(ui_language.tr('common.slot'))}</b> · <code>{html.escape(slot)}</code>",
            f"<b>{html.escape(ui_language.tr('workzone.role'))}</b> · {html.escape(role)}",
            f"<b>{html.escape(ui_language.tr('workzone.label'))}</b> · {html.escape(_slot_display_name(item))}",
            f"<b>{html.escape(ui_language.tr('workzone.directory'))}</b> · "
            f"<code>{html.escape(display_workzone_path(item['path']))}</code>",
            f"<b>{html.escape(ui_language.tr('workzone.accessible'))}</b> · "
            f"{html.escape(ui_language.tr('common.yes') if item['available'] else ui_language.tr('common.no'))}",
            "",
            html.escape(
                ui_language.tr(
                    "workzone.primary_effect" if slot == "main" else "workzone.attached_effect"
                )
            ),
        ]
    )
    return "\n".join(lines)


def workzone_overview_keyboard(state: Mapping[str, Any]) -> InlineKeyboardMarkup:
    normalized = normalize_workzone_state(state)
    configured = {item["slot_id"]: item for item in normalized["slots"]}
    revision = int(normalized["revision"])
    buttons = []
    for slot_id in _workzone_slot_ids():
        marker = _slot_marker(configured.get(slot_id), slot_id)
        label = ui_language.tr("workzone.main") if slot_id == "main" else slot_id
        buttons.append(
            InlineKeyboardButton(
                f"{marker} {label}", callback_data=f"wz:v:{revision}:{slot_id}"
            )
        )
    rows = [buttons[:5], buttons[5:]]
    rows.append(
        [
            InlineKeyboardButton(
                ui_language.tr("workzone.button.all_off"),
                callback_data=f"wz:a:{revision}",
            ),
            InlineKeyboardButton(
                ui_language.tr("common.refresh"), callback_data="wz:h"
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


def workzone_slot_keyboard(
    state: Mapping[str, Any], slot_id: str
) -> InlineKeyboardMarkup:
    normalized = normalize_workzone_state(state)
    slot = normalize_workzone_slot(slot_id)
    item = configured_workzone_slot(normalized, slot)
    revision = int(normalized["revision"])
    if item is None:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        ui_language.tr("workzone.button.set_path"),
                        callback_data=f"wz:p:{revision}:{slot}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        ui_language.tr("common.back"), callback_data="wz:h"
                    )
                ],
            ]
        )
    rows = [
        [
            InlineKeyboardButton(
                selected_label(ui_language.tr("common.on"), bool(item["enabled"])),
                callback_data=f"wz:on:{revision}:{slot}",
            ),
            InlineKeyboardButton(
                selected_label(ui_language.tr("common.off"), not bool(item["enabled"])),
                callback_data=f"wz:off:{revision}:{slot}",
            ),
        ],
        [
            InlineKeyboardButton(
                ui_language.tr("workzone.button.replace"),
                callback_data=f"wz:p:{revision}:{slot}",
            ),
            InlineKeyboardButton(
                ui_language.tr("workzone.button.reload"),
                callback_data=f"wz:reset:{revision}:{slot}",
            ),
        ],
        [
            InlineKeyboardButton(
                ui_language.tr("common.delete"),
                callback_data=f"wz:d:{revision}:{slot}",
            ),
            InlineKeyboardButton(ui_language.tr("common.back"), callback_data="wz:h"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def _delete_confirmation_keyboard(
    state: Mapping[str, Any], slot_id: str
) -> InlineKeyboardMarkup:
    revision = int(normalize_workzone_state(state)["revision"])
    slot = normalize_workzone_slot(slot_id)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    ui_language.tr("workzone.button.confirm_delete", slot=slot),
                    callback_data=f"wz:dc:{revision}:{slot}",
                )
            ],
            [
                InlineKeyboardButton(
                    ui_language.tr("workzone.button.keep"),
                    callback_data=f"wz:v:{revision}:{slot}",
                )
            ],
        ]
    )


async def _restart_backend_session(runtime: Any) -> None:
    backend = getattr(getattr(runtime, "backend_manager", None), "current_backend", None)
    if backend and getattr(getattr(backend, "capabilities", None), "supports_sessions", False):
        await backend.handle_new_session()


async def _activate_state(
    runtime: Any,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    force_reload: bool = False,
) -> None:
    before_topology = tuple(
        (item["slot_id"], item["path"], bool(item["available"]))
        for item in active_workzone_slots(before)
    )
    after_topology = tuple(
        (item["slot_id"], item["path"], bool(item["available"]))
        for item in active_workzone_slots(after)
    )
    install_runtime_state(runtime, after)
    runtime._sync_workzone_to_backend_config()
    if force_reload or before_topology != after_topology:
        await _restart_backend_session(runtime)


def _current_session(runtime: Any, update: Any) -> dict[str, Any]:
    from orchestrator import runtime_session

    return runtime_session.current_session_for_update(runtime, update)


async def _reply_overview(
    runtime: Any, update: Any, state: Mapping[str, Any], *, notice: str | None = None
) -> None:
    await runtime._reply_text(
        update,
        workzone_overview_text(runtime, state, notice=notice),
        parse_mode="HTML",
        reply_markup=workzone_overview_keyboard(state),
    )


async def _reply_slot(
    runtime: Any,
    update: Any,
    state: Mapping[str, Any],
    slot_id: str,
    *,
    notice: str | None = None,
) -> None:
    await runtime._reply_text(
        update,
        workzone_slot_text(runtime, state, slot_id, notice=notice),
        parse_mode="HTML",
        reply_markup=workzone_slot_keyboard(state, slot_id),
    )


async def _edit_overview(runtime: Any, query: Any, state: Mapping[str, Any], *, notice: str | None = None) -> None:
    await _safe_edit_message_text(
        query,
        workzone_overview_text(runtime, state, notice=notice),
        reply_markup=workzone_overview_keyboard(state),
    )


async def _edit_slot(
    runtime: Any,
    query: Any,
    state: Mapping[str, Any],
    slot_id: str,
    *,
    notice: str | None = None,
) -> None:
    await _safe_edit_message_text(
        query,
        workzone_slot_text(runtime, state, slot_id, notice=notice),
        reply_markup=workzone_slot_keyboard(state, slot_id),
    )


async def _safe_edit_message_text(
    query: Any, text: str, *, reply_markup: InlineKeyboardMarkup
) -> None:
    try:
        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def _pending_paths(runtime: Any) -> dict[tuple[int, int], dict[str, Any]]:
    pending = getattr(runtime, "_pending_workzone_paths", None)
    if not isinstance(pending, dict):
        pending = {}
        runtime._pending_workzone_paths = pending
    return pending


def _pending_key(update: Any) -> tuple[int, int]:
    return (
        int(getattr(getattr(update, "effective_chat", None), "id", 0) or 0),
        int(getattr(getattr(update, "effective_user", None), "id", 0) or 0),
    )


def _clear_pending_path(runtime: Any, update: Any) -> bool:
    return _pending_paths(runtime).pop(_pending_key(update), None) is not None


async def _begin_path_reply(
    runtime: Any,
    query: Any,
    *,
    session_id: str,
    state: Mapping[str, Any],
    slot_id: str,
) -> None:
    slot = normalize_workzone_slot(slot_id)
    prompt = await query.message.reply_text(
        ui_language.tr("workzone.path_prompt", slot=slot),
        reply_markup=ForceReply(
            selective=True,
            input_field_placeholder=ui_language.tr("workzone.path_placeholder"),
        ),
    )
    chat_id = int(getattr(getattr(query.message, "chat", None), "id", 0) or 0)
    user_id = int(getattr(getattr(query, "from_user", None), "id", 0) or 0)
    _pending_paths(runtime)[(chat_id, user_id)] = {
        "session_id": str(session_id),
        "slot": slot,
        "revision": int(normalize_workzone_state(state)["revision"]),
        "prompt_message_id": int(getattr(prompt, "message_id", 0) or 0),
        "expires_at": time.monotonic() + _PATH_REPLY_TTL_SECONDS,
    }


def _validate_unique_path(
    state: Mapping[str, Any], slot_id: str, path: Path
) -> None:
    slot = normalize_workzone_slot(slot_id)
    for item in normalize_workzone_state(state)["slots"]:
        if item["slot_id"] == slot:
            continue
        if Path(item["path"]).resolve() == path.resolve():
            raise ValueError(
                ui_language.tr("workzone.path_duplicate", slot=item["slot_id"])
            )


async def _save_path(
    runtime: Any,
    *,
    session_id: str,
    state: Mapping[str, Any],
    slot_id: str,
    raw_path: str,
    expected_revision: int | None,
    enable: bool | None,
    source: str,
) -> dict[str, Any]:
    from orchestrator import runtime_session

    slot = normalize_workzone_slot(slot_id)
    path = resolve_workzone_input(
        raw_path,
        runtime.global_config.project_root,
        runtime.workspace_dir,
    )
    _validate_unique_path(state, slot, path)
    current = configured_workzone_slot(state, slot)
    if enable is None:
        enable = bool(current["enabled"]) if current is not None else True
    store = runtime_session.ensure_store(runtime)
    updated = store.set_workzone_slot(
        str(session_id),
        slot,
        path=str(path),
        enabled=bool(enable),
        expected_revision=expected_revision,
        source=source,
    )
    after = normalize_workzone_state(updated)
    await _activate_state(runtime, state, after)
    return after


async def handle_pending_path_reply(runtime: Any, update: Any) -> bool:
    """Consume only the exact ForceReply message used for Workzone path entry."""

    pending = _pending_paths(runtime).get(_pending_key(update))
    if not pending:
        return False
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    reply = getattr(message, "reply_to_message", None)
    if int(getattr(reply, "message_id", 0) or 0) != int(pending["prompt_message_id"]):
        return False
    if time.monotonic() > float(pending["expires_at"]):
        _clear_pending_path(runtime, update)
        await runtime._reply_text(update, ui_language.tr("workzone.path_expired"))
        return True
    if runtime._backend_busy():
        await runtime._reply_text(update, ui_language.tr("workzone.busy"))
        return True
    session = _current_session(runtime, update)
    if str(session["session_id"]) != str(pending["session_id"]):
        _clear_pending_path(runtime, update)
        await runtime._reply_text(update, ui_language.tr("workzone.path_stale"))
        return True
    state = session_state(runtime, session["session_id"])
    try:
        after = await _save_path(
            runtime,
            session_id=session["session_id"],
            state=state,
            slot_id=pending["slot"],
            raw_path=str(getattr(message, "text", "") or ""),
            expected_revision=int(pending["revision"]),
            enable=None,
            source="telegram_force_reply",
        )
    except SessionConflict:
        _clear_pending_path(runtime, update)
        await runtime._reply_text(update, ui_language.tr("workzone.path_stale"))
        return True
    except ValueError as exc:
        await runtime._reply_text(
            update,
            ui_language.tr("workzone.not_changed", reason=html.escape(str(exc))),
            parse_mode="HTML",
        )
        return True
    _clear_pending_path(runtime, update)
    await _reply_slot(
        runtime,
        update,
        after,
        pending["slot"],
        notice=ui_language.tr("workzone.updated"),
    )
    return True


async def _set_enabled(
    runtime: Any,
    *,
    session_id: str,
    state: Mapping[str, Any],
    slot_id: str,
    enabled: bool,
    expected_revision: int | None = None,
    source: str = "telegram",
) -> dict[str, Any]:
    from orchestrator import runtime_session

    slot = normalize_workzone_slot(slot_id)
    item = configured_workzone_slot(state, slot)
    if item is None:
        raise ValueError(ui_language.tr("workzone.empty_slot", slot=slot))
    if enabled:
        resolve_workzone_input(
            item["path"], runtime.global_config.project_root, runtime.workspace_dir
        )
    updated = runtime_session.ensure_store(runtime).set_workzone_slot(
        str(session_id),
        slot,
        enabled=enabled,
        expected_revision=expected_revision,
        source=source,
    )
    after = normalize_workzone_state(updated)
    await _activate_state(runtime, state, after)
    return after


async def _delete_slot(
    runtime: Any,
    *,
    session_id: str,
    state: Mapping[str, Any],
    slot_id: str,
    expected_revision: int | None = None,
    source: str = "telegram",
) -> dict[str, Any]:
    from orchestrator import runtime_session

    updated = runtime_session.ensure_store(runtime).delete_workzone_slot(
        str(session_id),
        slot_id,
        expected_revision=expected_revision,
        source=source,
    )
    after = normalize_workzone_state(updated)
    await _activate_state(runtime, state, after)
    return after


async def _reload_slots(
    runtime: Any,
    *,
    session_id: str,
    state: Mapping[str, Any],
    slots: list[str],
    source: str,
) -> dict[str, Any]:
    from orchestrator import runtime_session

    for slot in slots:
        item = configured_workzone_slot(state, slot)
        if item is None:
            raise ValueError(ui_language.tr("workzone.empty_slot", slot=slot))
        resolve_workzone_input(
            item["path"], runtime.global_config.project_root, runtime.workspace_dir
        )
    runtime_session.ensure_store(runtime).record_workzone_reload(
        str(session_id), slots=slots, source=source
    )
    await _activate_state(
        runtime,
        state,
        normalize_workzone_state(state),
        force_reload=any(
            configured_workzone_slot(state, slot)["enabled"] for slot in slots
        ),
    )
    return normalize_workzone_state(state)


async def cmd_workzone(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    args = [str(item).strip() for item in (context.args or []) if str(item).strip()]
    session = _current_session(runtime, update)
    state = session_state(runtime, session["session_id"])
    install_runtime_state(runtime, state)
    if not args:
        await _reply_overview(runtime, update, state)
        return
    if args[0].lower() == "cancel":
        cleared = _clear_pending_path(runtime, update)
        await runtime._reply_text(
            update,
            ui_language.tr("workzone.path_cancelled" if cleared else "workzone.path_none"),
        )
        return
    if args[0].lower() == "all":
        action = args[1].lower() if len(args) > 1 else ""
        if action not in {"off", "reset", "reload"}:
            await runtime._reply_text(update, ui_language.tr("workzone.usage_all"))
            return
        if runtime._backend_busy():
            await runtime._reply_text(update, ui_language.tr("workzone.busy"))
            return
        try:
            if action == "off":
                from orchestrator import runtime_session

                updated = runtime_session.ensure_store(runtime).disable_all_workzones(
                    session["session_id"], source="telegram_command"
                )
                after = normalize_workzone_state(updated)
                await _activate_state(runtime, state, after)
                await _reply_overview(
                    runtime,
                    update,
                    after,
                    notice=ui_language.tr("workzone.all_off"),
                )
                return
            configured = [item["slot_id"] for item in state["slots"]]
            if not configured:
                raise ValueError(ui_language.tr("workzone.none_configured"))
            after = await _reload_slots(
                runtime,
                session_id=session["session_id"],
                state=state,
                slots=configured,
                source="telegram_command",
            )
            await _reply_overview(
                runtime,
                update,
                after,
                notice=ui_language.tr("workzone.reloaded"),
            )
        except ValueError as exc:
            await runtime._reply_text(
                update,
                ui_language.tr("workzone.not_changed", reason=html.escape(str(exc))),
                parse_mode="HTML",
            )
        return

    first = args[0].lower()
    if first in {"main", "default", "0", *[str(number) for number in range(1, 10)]}:
        slot = normalize_workzone_slot(first)
        rest = args[1:]
    else:
        slot = "main"
        rest = args
    if not rest:
        await _reply_slot(runtime, update, state, slot)
        return
    if runtime._backend_busy():
        await runtime._reply_text(update, ui_language.tr("workzone.busy"))
        return
    action = rest[0].lower()
    try:
        if action in {"on", "off"}:
            after = await _set_enabled(
                runtime,
                session_id=session["session_id"],
                state=state,
                slot_id=slot,
                enabled=action == "on",
                source="telegram_command",
            )
            notice = ui_language.tr(
                "workzone.turned_on" if action == "on" else "workzone.turned_off",
                slot=slot,
            )
            await _reply_slot(runtime, update, after, slot, notice=notice)
            return
        if action in {"reset", "reload"}:
            after = await _reload_slots(
                runtime,
                session_id=session["session_id"],
                state=state,
                slots=[slot],
                source="telegram_command",
            )
            await _reply_slot(
                runtime,
                update,
                after,
                slot,
                notice=ui_language.tr("workzone.reloaded"),
            )
            return
        if action == "delete":
            confirmed = len(rest) > 1 and rest[1].upper() == "CONFIRM"
            if not confirmed:
                item = configured_workzone_slot(state, slot)
                if item is None:
                    raise ValueError(ui_language.tr("workzone.empty_slot", slot=slot))
                await runtime._reply_text(
                    update,
                    confirm_card(
                        "⚠️",
                        ui_language.tr("workzone.delete_title"),
                        target=f"<code>{html.escape(slot)}</code>",
                        consequence=ui_language.tr("workzone.delete_effect"),
                    ),
                    parse_mode="HTML",
                    reply_markup=_delete_confirmation_keyboard(state, slot),
                )
                return
            after = await _delete_slot(
                runtime,
                session_id=session["session_id"],
                state=state,
                slot_id=slot,
                source="telegram_command",
            )
            await _reply_overview(
                runtime,
                update,
                after,
                notice=ui_language.tr("workzone.deleted", slot=slot),
            )
            return
        if action == "label":
            label = " ".join(rest[1:]).strip()
            item = configured_workzone_slot(state, slot)
            if item is None:
                raise ValueError(ui_language.tr("workzone.empty_slot", slot=slot))
            from orchestrator import runtime_session

            updated = runtime_session.ensure_store(runtime).set_workzone_slot(
                session["session_id"],
                slot,
                label=label,
                source="telegram_command",
            )
            after = normalize_workzone_state(updated)
            await _activate_state(runtime, state, after)
            await _reply_slot(
                runtime,
                update,
                after,
                slot,
                notice=ui_language.tr("workzone.label_updated"),
            )
            return
        if action in {"replace", "set"}:
            raw_path = " ".join(rest[1:]).strip()
            if not raw_path:
                raise ValueError(ui_language.tr("workzone.path_missing"))
            enable = True if action == "set" else None
        else:
            raw_path = " ".join(rest).strip()
            enable = True
        after = await _save_path(
            runtime,
            session_id=session["session_id"],
            state=state,
            slot_id=slot,
            raw_path=raw_path,
            expected_revision=None,
            enable=enable,
            source="telegram_command",
        )
        await _reply_slot(
            runtime,
            update,
            after,
            slot,
            notice=ui_language.tr("workzone.updated"),
        )
    except (ValueError, SessionConflict) as exc:
        await runtime._reply_text(
            update,
            ui_language.tr("workzone.not_changed", reason=html.escape(str(exc))),
            parse_mode="HTML",
        )


async def callback_workzone(runtime: Any, update: Any, context: Any) -> None:
    del context
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        return
    data = str(query.data or "")
    session = _current_session(runtime, update)
    state = session_state(runtime, session["session_id"])
    if data == "wz:h":
        await query.answer()
        await _edit_overview(runtime, query, state)
        return
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "wz":
        await query.answer()
        return
    action = parts[1]
    try:
        expected_revision = int(parts[2])
    except ValueError:
        await query.answer(ui_language.tr("workzone.menu_stale"), show_alert=True)
        return
    if expected_revision != int(state["revision"]):
        await query.answer(ui_language.tr("workzone.menu_stale"), show_alert=True)
        await _edit_overview(runtime, query, state)
        return
    try:
        slot = normalize_workzone_slot(parts[3]) if len(parts) > 3 else None
    except ValueError:
        await query.answer(ui_language.tr("workzone.menu_stale"), show_alert=True)
        return
    if action == "v" and slot is not None:
        await query.answer()
        await _edit_slot(runtime, query, state, slot)
        return
    if action == "d" and slot is not None:
        item = configured_workzone_slot(state, slot)
        if item is None:
            await query.answer(ui_language.tr("workzone.empty_slot", slot=slot), show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(
            confirm_card(
                "⚠️",
                ui_language.tr("workzone.delete_title"),
                target=f"<code>{html.escape(slot)}</code>",
                consequence=ui_language.tr("workzone.delete_effect"),
            ),
            parse_mode="HTML",
            reply_markup=_delete_confirmation_keyboard(state, slot),
        )
        return
    if action == "p" and slot is not None:
        if runtime._backend_busy():
            await query.answer(ui_language.tr("workzone.busy"), show_alert=True)
            return
        await _begin_path_reply(
            runtime,
            query,
            session_id=session["session_id"],
            state=state,
            slot_id=slot,
        )
        await query.answer(ui_language.tr("workzone.path_waiting"))
        return
    if runtime._backend_busy():
        await query.answer(ui_language.tr("workzone.busy"), show_alert=True)
        return
    try:
        if action in {"on", "off"} and slot is not None:
            after = await _set_enabled(
                runtime,
                session_id=session["session_id"],
                state=state,
                slot_id=slot,
                enabled=action == "on",
                expected_revision=expected_revision,
                source="telegram_callback",
            )
            await query.answer(ui_language.tr("workzone.updated"))
            await _edit_slot(runtime, query, after, slot)
            return
        if action == "reset" and slot is not None:
            after = await _reload_slots(
                runtime,
                session_id=session["session_id"],
                state=state,
                slots=[slot],
                source="telegram_callback",
            )
            await query.answer(ui_language.tr("workzone.reloaded"))
            await _edit_slot(runtime, query, after, slot)
            return
        if action == "dc" and slot is not None:
            after = await _delete_slot(
                runtime,
                session_id=session["session_id"],
                state=state,
                slot_id=slot,
                expected_revision=expected_revision,
                source="telegram_callback",
            )
            await query.answer(ui_language.tr("workzone.deleted", slot=slot))
            await _edit_overview(runtime, query, after)
            return
        if action == "a":
            from orchestrator import runtime_session

            updated = runtime_session.ensure_store(runtime).disable_all_workzones(
                session["session_id"],
                expected_revision=expected_revision,
                source="telegram_callback",
            )
            after = normalize_workzone_state(updated)
            await _activate_state(runtime, state, after)
            await query.answer(ui_language.tr("workzone.all_off"))
            await _edit_overview(runtime, query, after)
            return
    except SessionConflict:
        current = session_state(runtime, session["session_id"])
        await query.answer(ui_language.tr("workzone.menu_stale"), show_alert=True)
        await _edit_overview(runtime, query, current)
        return
    except ValueError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await query.answer()
