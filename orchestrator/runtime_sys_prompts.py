from __future__ import annotations

import hashlib
import html
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import runtime_menu_views
from orchestrator.bridge_memory import SysPromptManager, global_sys_prompt_state_path
from orchestrator.command_ui import confirm_card, selected_label
from orchestrator.slash_command_audit import (
    append_audit_record,
    build_audit_record,
)

GLOBAL_SCOPE_ALIASES = frozenset({"global", "g"})
LOCAL_SCOPE_ALIASES = frozenset({"local", "l"})
logger = logging.getLogger("BridgeU.SysPrompt")


def _is_authorized(runtime: Any, user_id: int | None) -> bool:
    checker = getattr(runtime, "_is_authorized_user", None)
    if callable(checker):
        return bool(checker(user_id))
    return user_id is not None and user_id == getattr(
        runtime.global_config, "authorized_id", None
    )


def _instance_id(runtime: Any) -> str:
    return str(getattr(runtime.global_config, "instance_id", None) or "HASHI").upper()


def _resolve_scope(args: list[str]) -> tuple[str, list[str]]:
    if not args:
        return "local", []
    first = args[0].lower()
    if first in GLOBAL_SCOPE_ALIASES:
        return "global", args[1:]
    if first in LOCAL_SCOPE_ALIASES:
        return "local", args[1:]
    return "local", args


def _manager_for_scope(runtime: Any, scope: str) -> SysPromptManager:
    if scope != "global":
        return runtime.sys_prompt_manager
    manager = getattr(runtime, "global_sys_prompt_manager", None)
    if manager is None:
        manager = SysPromptManager.for_instance(runtime.global_config)
        runtime.global_sys_prompt_manager = manager
        assembler = getattr(runtime, "context_assembler", None)
        if assembler is not None:
            assembler.global_sys_prompt_manager = manager
    return manager


def _text_hash(slot_state: dict[str, object]) -> str:
    text = str(slot_state.get("text") or "")
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def _record_global_mutation(
    runtime: Any,
    *,
    actor_id: int | None,
    action: str,
    slot: str,
    before: dict[str, object],
    after: dict[str, object],
    source_channel: str,
) -> None:
    try:
        state_path = global_sys_prompt_state_path(runtime.global_config)
        append_audit_record(
            state_path.with_name("global_sys_prompt_audit.jsonl"),
            build_audit_record(
                agent=str(getattr(runtime, "name", None) or "unknown"),
                command_name="sys_global_mutation",
                args=[
                    f"action={action}",
                    f"slot={slot}",
                    f"before_active={bool(before.get('active'))}",
                    f"after_active={bool(after.get('active'))}",
                    f"before_sha256={_text_hash(before)}",
                    f"after_sha256={_text_hash(after)}",
                ],
                source_channel=source_channel,
                handler_kind="runtime_sys_prompts",
                status="success",
                duration_ms=0,
                actor_id=actor_id,
                side_effects=["global_sys_prompts_updated"],
            ),
        )
    except Exception as exc:  # noqa: BLE001 - prompt changes must survive audit I/O failure
        logger.warning("Could not append global /sys audit record: %s", exc)


def _mutate_slot(
    runtime: Any,
    manager: SysPromptManager,
    *,
    scope: str,
    slot: str,
    action: str,
    actor_id: int | None,
    source_channel: str,
    text: str | None = None,
) -> str:
    before = manager.get_slot(slot)
    if action == "on":
        notice = manager.activate(slot)
    elif action == "off":
        notice = manager.deactivate(slot)
    elif action == "delete":
        notice = manager.delete(slot)
    elif action == "save":
        notice = manager.save(slot, text or "")
    elif action == "replace":
        notice = manager.replace(slot, text or "")
    else:
        raise ValueError(f"unsupported /sys mutation: {action}")
    after = manager.get_slot(slot)
    if scope == "global":
        _record_global_mutation(
            runtime,
            actor_id=actor_id,
            action=action,
            slot=slot,
            before=before,
            after=after,
            source_channel=source_channel,
        )
    return notice


def _scope_keyboard_row(runtime: Any, scope: str) -> list[InlineKeyboardButton]:
    instance_id = _instance_id(runtime)
    return [
        InlineKeyboardButton(
            selected_label("👤 Local", scope == "local"),
            callback_data="sys:menu:local",
        ),
        InlineKeyboardButton(
            selected_label(f"🌐 {instance_id}", scope == "global"),
            callback_data="sys:menu:global",
        ),
    ]


def sys_slots_keyboard(runtime: Any, scope: str) -> InlineKeyboardMarkup:
    manager = _manager_for_scope(runtime, scope)
    buttons: list[InlineKeyboardButton] = []
    for item in manager.list_slots():
        slot = str(item["slot"])
        if item["active"]:
            marker = "●"
        elif item["text"]:
            marker = "○"
        else:
            marker = "·"
        buttons.append(
            InlineKeyboardButton(
                f"{marker} {slot}",
                callback_data=f"sys:view:{scope}:{slot}",
            )
        )
    rows: list[list[InlineKeyboardButton]] = [_scope_keyboard_row(runtime, scope)]
    rows.extend([buttons[index : index + 5] for index in range(0, len(buttons), 5)])
    return InlineKeyboardMarkup(rows)


def sys_slot_keyboard(runtime: Any, scope: str, slot: str) -> InlineKeyboardMarkup:
    manager = _manager_for_scope(runtime, scope)
    active = bool(manager.get_slot(slot).get("active"))
    rows = [
        _scope_keyboard_row(runtime, scope),
        [
            InlineKeyboardButton(
                selected_label("On", active),
                callback_data=f"sys:on:{scope}:{slot}",
            ),
            InlineKeyboardButton(
                selected_label("Off", not active),
                callback_data=f"sys:off:{scope}:{slot}",
            ),
        ],
        [
            InlineKeyboardButton("Output", callback_data=f"sys:output:{scope}:{slot}"),
            InlineKeyboardButton("Delete", callback_data=f"sys:delete:{scope}:{slot}"),
        ],
        [InlineKeyboardButton("← Slots", callback_data=f"sys:menu:{scope}")],
    ]
    return InlineKeyboardMarkup(rows)


def _confirm_keyboard(
    runtime: Any,
    *,
    action: str,
    scope: str,
    slot: str,
) -> InlineKeyboardMarkup:
    if action == "on" and scope == "global":
        confirm_label = f"Activate across {_instance_id(runtime)}"
    elif action == "delete" and scope == "global":
        confirm_label = f"Delete global slot {slot}"
    else:
        confirm_label = f"Delete local slot {slot}"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    confirm_label,
                    callback_data=f"sys:confirm_{action}:{scope}:{slot}",
                )
            ],
            [
                InlineKeyboardButton(
                    "← Keep current state", callback_data=f"sys:view:{scope}:{slot}"
                )
            ],
        ]
    )


def _slots_text(runtime: Any, scope: str) -> str:
    return runtime_menu_views.sys_slots_text(
        _manager_for_scope(runtime, scope),
        scope=scope,
        instance_id=_instance_id(runtime),
    )


def _slot_text(runtime: Any, scope: str, slot: str) -> str:
    return runtime_menu_views.sys_slot_text(
        _manager_for_scope(runtime, scope),
        slot,
        scope=scope,
        instance_id=_instance_id(runtime),
    )


async def _reply(runtime: Any, update: Any, text: str, **kwargs: Any) -> Any:
    helper = getattr(runtime, "_reply_text", None)
    if callable(helper):
        return await helper(update, text, **kwargs)
    return await update.message.reply_text(text, **kwargs)


async def _reply_slots(runtime: Any, update: Any, scope: str) -> None:
    await _reply(
        runtime,
        update,
        _slots_text(runtime, scope),
        parse_mode="HTML",
        reply_markup=sys_slots_keyboard(runtime, scope),
    )


async def _reply_slot(
    runtime: Any,
    update: Any,
    scope: str,
    slot: str,
    *,
    notice: str | None = None,
) -> None:
    text = _slot_text(runtime, scope, slot)
    if notice:
        text = f"{html.escape(notice)}\n\n{text}"
    await _reply(
        runtime,
        update,
        text,
        parse_mode="HTML",
        reply_markup=sys_slot_keyboard(runtime, scope, slot),
    )


async def _reply_confirmation(
    runtime: Any, update: Any, scope: str, slot: str, action: str
) -> None:
    if action == "on":
        title = "Activate global system prompt"
        consequence = (
            f"This activates the slot for every configured Agent in "
            f"<code>{html.escape(_instance_id(runtime))}</code> on its next request."
        )
    else:
        title = "Delete system prompt"
        scope_text = (
            "every Agent in this HASHI instance" if scope == "global" else "this Agent"
        )
        consequence = f"This permanently clears the configured text and disables the slot for {scope_text}."
    await _reply(
        runtime,
        update,
        confirm_card(
            "⚠️",
            title,
            target=f"<code>{html.escape(scope)} / {html.escape(slot)}</code>",
            consequence=consequence,
        ),
        parse_mode="HTML",
        reply_markup=_confirm_keyboard(runtime, action=action, scope=scope, slot=slot),
    )


def _usage() -> str:
    return (
        "Usage:\n"
        "/sys — show local slots\n"
        "/sys <n> — show local slot\n"
        "/sys <n> on|off|delete\n"
        "/sys <n> save|replace <message>\n"
        "/sys output <n>\n\n"
        "/sys global … — manage instance-global slots\n"
        "/sys g … — short alias for /sys global …"
    )


async def cmd_sys(runtime: Any, update: Any, context: Any) -> None:
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    if not _is_authorized(runtime, user_id):
        return
    raw_args = [
        str(arg).strip()
        for arg in (getattr(context, "args", None) or [])
        if str(arg).strip()
    ]
    scope, args = _resolve_scope(raw_args)
    manager = _manager_for_scope(runtime, scope)

    if not args:
        await _reply_slots(runtime, update, scope)
        return

    if args[0].lower() == "output":
        slot = args[1] if len(args) > 1 else ""
        if slot not in manager.SLOTS:
            await _reply(
                runtime,
                update,
                f"Usage: {'/sys global' if scope == 'global' else '/sys'} output <1-10>",
            )
            return
        text = str(manager.get_slot(slot).get("text") or "")
        await _reply(runtime, update, text if text else "(empty)", parse_mode=None)
        return

    slot = args[0]
    if slot not in manager.SLOTS:
        await _reply(
            runtime, update, f"Invalid slot '{slot}'. Use 1-10, /sys global, or /sys g."
        )
        return

    if len(args) == 1:
        await _reply_slot(runtime, update, scope, slot)
        return

    action = args[1].lower()
    confirmed = len(args) > 2 and args[2].upper() == "CONFIRM"

    if action == "on":
        if not manager.get_slot(slot).get("text"):
            await _reply(
                runtime, update, f"Slot {slot} is empty — save a message first."
            )
            return
        if scope == "global" and not confirmed:
            await _reply_confirmation(runtime, update, scope, slot, "on")
            return
        notice = _mutate_slot(
            runtime,
            manager,
            scope=scope,
            slot=slot,
            action="on",
            actor_id=user_id,
            source_channel="slash_command",
        )
        await _reply_slot(runtime, update, scope, slot, notice=notice)
        return

    if action == "off":
        notice = _mutate_slot(
            runtime,
            manager,
            scope=scope,
            slot=slot,
            action="off",
            actor_id=user_id,
            source_channel="slash_command",
        )
        await _reply_slot(runtime, update, scope, slot, notice=notice)
        return

    if action == "delete":
        if scope == "global" and manager.get_slot(slot).get("text") and not confirmed:
            await _reply_confirmation(runtime, update, scope, slot, "delete")
            return
        notice = _mutate_slot(
            runtime,
            manager,
            scope=scope,
            slot=slot,
            action="delete",
            actor_id=user_id,
            source_channel="slash_command",
        )
        await _reply_slot(runtime, update, scope, slot, notice=notice)
        return

    if action == "save":
        if scope == "global" and manager.get_slot(slot).get("text"):
            await _reply(
                runtime,
                update,
                f"Global slot {slot} is already configured. Use /sys global {slot} "
                "replace <message>; an active slot also requires CONFIRM.",
            )
            return
        text = " ".join(args[2:]).strip()
        if not text:
            prefix = "/sys global" if scope == "global" else "/sys"
            await _reply(runtime, update, f"Usage: {prefix} <slot> save <message>")
            return
        notice = _mutate_slot(
            runtime,
            manager,
            scope=scope,
            slot=slot,
            action="save",
            actor_id=user_id,
            source_channel="slash_command",
            text=text,
        )
        await _reply_slot(runtime, update, scope, slot, notice=notice)
        return

    if action == "replace":
        text_args = args[2:]
        is_active_global = scope == "global" and bool(
            manager.get_slot(slot).get("active")
        )
        if is_active_global and not confirmed:
            await _reply(
                runtime,
                update,
                "This global slot is active. To change every Agent immediately, use "
                f"/sys global {slot} replace CONFIRM <message>, or turn it off first.",
            )
            return
        if is_active_global and confirmed:
            text_args = text_args[1:]
        text = " ".join(text_args).strip()
        if not text:
            prefix = "/sys global" if scope == "global" else "/sys"
            await _reply(runtime, update, f"Usage: {prefix} <slot> replace <message>")
            return
        notice = _mutate_slot(
            runtime,
            manager,
            scope=scope,
            slot=slot,
            action="replace",
            actor_id=user_id,
            source_channel="slash_command",
            text=text,
        )
        await _reply_slot(runtime, update, scope, slot, notice=notice)
        return

    await _reply(runtime, update, _usage())


async def _edit_slots(runtime: Any, query: Any, scope: str) -> None:
    await query.edit_message_text(
        _slots_text(runtime, scope),
        parse_mode="HTML",
        reply_markup=sys_slots_keyboard(runtime, scope),
    )


async def _edit_slot(
    runtime: Any,
    query: Any,
    scope: str,
    slot: str,
    *,
    notice: str | None = None,
) -> None:
    text = _slot_text(runtime, scope, slot)
    if notice:
        text = f"{html.escape(notice)}\n\n{text}"
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=sys_slot_keyboard(runtime, scope, slot),
    )


async def callback_sys(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    user_id = getattr(getattr(query, "from_user", None), "id", None)
    if not _is_authorized(runtime, user_id):
        await query.answer()
        return
    allowed = getattr(runtime, "_is_command_allowed", None)
    if callable(allowed) and not allowed("sys"):
        await query.answer("/sys is disabled for this Agent.", show_alert=True)
        return

    parts = str(getattr(query, "data", None) or "").split(":")
    if len(parts) not in {3, 4} or parts[0] != "sys":
        await query.answer("Invalid /sys action.", show_alert=True)
        return
    action = parts[1]
    scope = parts[2]
    slot = parts[3] if len(parts) == 4 else ""
    if scope not in {"local", "global"}:
        await query.answer("Invalid /sys scope.", show_alert=True)
        return
    manager = _manager_for_scope(runtime, scope)

    if action == "menu" and not slot:
        await _edit_slots(runtime, query, scope)
        await query.answer()
        return
    if slot not in manager.SLOTS:
        await query.answer("Invalid /sys slot.", show_alert=True)
        return
    if action == "view":
        await _edit_slot(runtime, query, scope, slot)
    elif action == "output":
        text = str(manager.get_slot(slot).get("text") or "")
        await query.message.reply_text(text if text else "(empty)", parse_mode=None)
    elif action == "on":
        if not manager.get_slot(slot).get("text"):
            await query.answer("Save a message first.", show_alert=True)
            return
        if scope == "global":
            await query.edit_message_text(
                confirm_card(
                    "⚠️",
                    "Activate global system prompt",
                    target=f"<code>global / {html.escape(slot)}</code>",
                    consequence=(
                        f"This activates the slot for every configured Agent in "
                        f"<code>{html.escape(_instance_id(runtime))}</code> on its next request."
                    ),
                ),
                parse_mode="HTML",
                reply_markup=_confirm_keyboard(
                    runtime, action="on", scope=scope, slot=slot
                ),
            )
        else:
            notice = _mutate_slot(
                runtime,
                manager,
                scope=scope,
                slot=slot,
                action="on",
                actor_id=user_id,
                source_channel="telegram_callback",
            )
            await _edit_slot(runtime, query, scope, slot, notice=notice)
    elif action == "confirm_on" and scope == "global":
        notice = _mutate_slot(
            runtime,
            manager,
            scope=scope,
            slot=slot,
            action="on",
            actor_id=user_id,
            source_channel="telegram_callback",
        )
        await _edit_slot(runtime, query, scope, slot, notice=notice)
    elif action == "off":
        notice = _mutate_slot(
            runtime,
            manager,
            scope=scope,
            slot=slot,
            action="off",
            actor_id=user_id,
            source_channel="telegram_callback",
        )
        await _edit_slot(runtime, query, scope, slot, notice=notice)
    elif action == "delete":
        await query.edit_message_text(
            confirm_card(
                "⚠️",
                "Delete system prompt",
                target=f"<code>{html.escape(scope)} / {html.escape(slot)}</code>",
                consequence=(
                    "This permanently clears the configured text and disables the slot for "
                    + (
                        "every Agent in this HASHI instance."
                        if scope == "global"
                        else "this Agent."
                    )
                ),
            ),
            parse_mode="HTML",
            reply_markup=_confirm_keyboard(
                runtime, action="delete", scope=scope, slot=slot
            ),
        )
    elif action == "confirm_delete":
        notice = _mutate_slot(
            runtime,
            manager,
            scope=scope,
            slot=slot,
            action="delete",
            actor_id=user_id,
            source_channel="telegram_callback",
        )
        await _edit_slot(runtime, query, scope, slot, notice=notice)
    else:
        await query.answer("Invalid /sys action.", show_alert=True)
        return
    await query.answer()
