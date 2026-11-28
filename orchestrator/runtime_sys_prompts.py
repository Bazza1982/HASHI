from __future__ import annotations

import hashlib
import html
import logging
import re
from types import SimpleNamespace
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from pathlib import Path

from orchestrator import runtime_menu_views, ui_language
from orchestrator.agent_slot_transfer import (
    SourceSlot,
    SlotTransfer,
    plan_slot_transfer,
)
from orchestrator.bridge_memory import (
    SysPromptManager,
    SysPromptSemanticConflict,
    global_sys_prompt_state_path,
)
from orchestrator.command_ui import confirm_card, selected_label
from orchestrator.config_json import ConfigConflictError, ConfigDurabilityError
from orchestrator.slash_command_audit import (
    append_audit_record,
    build_audit_record,
    parse_slash_command_text,
)
from orchestrator.sys_command_contract import (
    SYS_GLOBAL_SCOPE_ALIASES,
    SYS_LOCAL_SCOPE_ALIASES,
    SysCommandContractError,
    normalize_sys_arguments,
)

# One definition, shared with the authorization gate in the API layer.
GLOBAL_SCOPE_ALIASES = SYS_GLOBAL_SCOPE_ALIASES
LOCAL_SCOPE_ALIASES = SYS_LOCAL_SCOPE_ALIASES
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
        manager.activate(slot, expected_slot=before)
        notice_key = "activated"
    elif action == "off":
        manager.deactivate(slot, expected_slot=before)
        notice_key = "deactivated"
    elif action == "delete":
        manager.delete(slot, expected_slot=before)
        notice_key = "cleared"
    elif action == "save":
        manager.save(slot, text or "", expected_slot=before)
        notice_key = "saved"
    elif action == "replace":
        manager.replace(slot, text or "", expected_slot=before)
        notice_key = "updated"
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
    return ui_language.tr(f"sys.notice.{notice_key}.{scope}", slot=slot)


def _scope_keyboard_row(runtime: Any, scope: str) -> list[InlineKeyboardButton]:
    instance_id = _instance_id(runtime)
    return [
        InlineKeyboardButton(
            selected_label(ui_language.tr("sys.button.local"), scope == "local"),
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
                selected_label(ui_language.tr("common.on"), active),
                callback_data=f"sys:on:{scope}:{slot}",
            ),
            InlineKeyboardButton(
                selected_label(ui_language.tr("common.off"), not active),
                callback_data=f"sys:off:{scope}:{slot}",
            ),
        ],
        [
            InlineKeyboardButton(ui_language.tr("common.output"), callback_data=f"sys:output:{scope}:{slot}"),
            InlineKeyboardButton(ui_language.tr("common.delete"), callback_data=f"sys:delete:{scope}:{slot}"),
        ],
        [InlineKeyboardButton(ui_language.tr("sys.button.slots"), callback_data=f"sys:menu:{scope}")],
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
        confirm_label = ui_language.tr(
            "sys.button.activate_global", instance=_instance_id(runtime)
        )
    elif action == "delete" and scope == "global":
        confirm_label = ui_language.tr("sys.button.delete_global", slot=slot)
    else:
        confirm_label = ui_language.tr("sys.button.delete_local", slot=slot)
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
                    ui_language.tr("sys.button.keep_state"),
                    callback_data=f"sys:view:{scope}:{slot}",
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


async def _reply_from_callback(
    runtime: Any,
    update: Any,
    query: Any,
    text: str,
    **kwargs: Any,
) -> Any:
    """Project a callback follow-up through the canonical reply owner."""

    helper = getattr(runtime, "_reply_text", None)
    if not callable(helper):
        return await query.message.reply_text(text, **kwargs)
    projected = SimpleNamespace(
        update_id=getattr(update, "update_id", None),
        effective_user=getattr(update, "effective_user", None)
        or getattr(query, "from_user", None),
        effective_chat=getattr(update, "effective_chat", None)
        or getattr(query.message, "chat", None),
        message=query.message,
    )
    for attribute in (
        "_hashi_session_surface",
        "_hashi_session_channel_key",
        "_hashi_session_id",
        "_hashi_session_owner_id",
        "_hashi_ui_locale",
    ):
        if hasattr(update, attribute):
            setattr(projected, attribute, getattr(update, attribute))
    return await helper(projected, text, **kwargs)


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
        consequence = ui_language.tr(
            "sys.confirm.activate_consequence",
            instance=html.escape(_instance_id(runtime)),
        )
    else:
        title = "Delete system prompt"
        consequence = ui_language.tr(
            "sys.confirm.delete_consequence_global"
            if scope == "global"
            else "sys.confirm.delete_consequence_local"
        )
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


_SYS_TRANSFER_VERBS = frozenset({"copy", "import", "clear"})


def sibling_agent_names(runtime: Any) -> list[str]:
    from orchestrator import runtime_workzone

    return runtime_workzone.sibling_agent_names(runtime)


def agent_workspace_dir(runtime: Any, agent_name: str) -> Path:
    """Resolve one sibling Agent's workspace from its configured directory."""

    from orchestrator import runtime_workzone

    return runtime_workzone.agent_workspace_dir(runtime, agent_name)


def local_manager_for(runtime: Any, agent_name: str) -> SysPromptManager:
    """Return the local (per-Agent) /sys manager for one Agent."""

    if str(agent_name) == str(runtime.name):
        return runtime.sys_prompt_manager
    return SysPromptManager(
        state_path=agent_workspace_dir(runtime, agent_name) / "sys_prompts.json",
        scope="local",
    )


def _resolve_agent(runtime: Any, raw_name: str) -> str:
    wanted = str(raw_name or "").strip()
    known = {name.casefold(): name for name in sibling_agent_names(runtime)}
    resolved = known.get(wanted.casefold()) if wanted else None
    if resolved is None:
        raise ValueError(ui_language.tr("sys.transfer.unknown_agent", agent=wanted))
    return resolved


def _sys_transfer_receipt_text(
    *,
    verb: str,
    source_agent: str,
    target_agent: str,
    plan: list[SlotTransfer],
    applied: list[SlotTransfer] | None = None,
    conflicts: list[str] | None = None,
) -> str:
    title = ui_language.tr(
        "sys.transfer.title_copy" if verb == "copy" else "sys.transfer.title_import",
        source=source_agent,
        target=target_agent,
    )
    lines = [f"<b>{html.escape(title)}</b>"]
    written = (
        list(applied)
        if applied is not None
        else [item for item in plan if item.outcome == "copied"]
    )
    for item in written:
        lines.append(
            ui_language.tr(
                "sys.transfer.mapping",
                source=f"<code>{html.escape(item.source_slot)}</code>",
                target=f"<code>{html.escape(str(item.target_slot))}</code>",
                state=ui_language.tr(
                    "sys.active_suffix" if item.enabled else "sys.inactive_suffix"
                ),
            )
        )
    for item in plan:
        if item.outcome == "duplicate":
            lines.append(
                ui_language.tr(
                    "sys.transfer.duplicate",
                    slot=f"<code>{html.escape(item.source_slot)}</code>",
                    agent=target_agent,
                )
            )
        elif item.outcome == "no_free_slot":
            lines.append(
                ui_language.tr(
                    "sys.transfer.no_slot",
                    slot=f"<code>{html.escape(item.source_slot)}</code>",
                    agent=target_agent,
                )
            )
    for source_slot in conflicts or ():
        lines.append(
            ui_language.tr(
                "sys.transfer.conflict",
                slot=f"<code>{html.escape(source_slot)}</code>",
                agent=target_agent,
            )
        )
    if not written:
        lines.append(ui_language.tr("sys.transfer.nothing_copied", agent=target_agent))
    return "\n".join(lines)


async def _cmd_sys_transfer(
    runtime: Any,
    update: Any,
    *,
    scope: str,
    args: list[str],
    actor_id: int | None,
) -> dict[str, Any] | None:
    verb = args[0].lower()
    if scope != "local":
        return await _reject(
            runtime,
            update,
            "sys_transfer_local_only",
            ui_language.tr("sys.error.transfer_local_only"),
        )
    if verb == "clear":
        if len(args) > 1:
            return await _reject(
                runtime, update, "sys_transfer_usage", ui_language.tr("sys.transfer.usage")
            )
        manager = runtime.sys_prompt_manager
        observed: dict[str, dict[str, object]] = {}
        for slot in SysPromptManager.SLOTS:
            # Read each slot exactly once: the snapshot used to decide what to
            # delete must be the same read, or a write landing between two reads
            # would be deleted as if it had been enumerated.
            snapshot = dict(manager.get_slot(slot))
            if str(snapshot.get("text") or "").strip():
                observed[slot] = snapshot
        if not observed:
            await _reply(
                runtime,
                update,
                ui_language.tr("sys.clear.empty", agent=str(runtime.name)),
            )
            return None
        cleared = 0
        changed: list[str] = []
        for slot, snapshot in observed.items():
            # Clear only the text that was enumerated; a slot rewritten after
            # this observation must survive instead of being deleted blindly.
            try:
                manager.delete(slot, expected_slot=snapshot)
            except SysPromptSemanticConflict:
                changed.append(slot)
            else:
                cleared += 1
        message = ui_language.tr(
            "sys.clear.done", count=cleared, agent=str(runtime.name)
        )
        if changed:
            message = "\n".join(
                [
                    message,
                    ui_language.tr(
                        "sys.clear.changed", slots=", ".join(sorted(changed))
                    ),
                ]
            )
        await _reply(runtime, update, message)
        return None

    if len(args) < 2 or len(args) > 3:
        return await _reject(
            runtime, update, "sys_transfer_usage", ui_language.tr("sys.transfer.usage")
        )
    slot: str | None = None
    if len(args) == 3:
        slot = str(args[2]).strip()
        if slot not in SysPromptManager.SLOTS:
            return await _reject(
                runtime,
                update,
                "sys_slot_invalid",
                ui_language.tr("sys.error.invalid_slot", slot=slot),
            )
    try:
        other = _resolve_agent(runtime, args[1])
    except ValueError as exc:
        return await _reject(runtime, update, "sys_transfer_unknown_agent", str(exc))
    if verb == "copy":
        source_agent, target_agent = str(runtime.name), other
    else:
        source_agent, target_agent = other, str(runtime.name)
    if source_agent == target_agent:
        return await _reject(
            runtime,
            update,
            "sys_transfer_same_agent",
            ui_language.tr("sys.transfer.same_agent", agent=source_agent),
        )

    from orchestrator.runtime_workzone import AgentWorkspaceUnavailable

    try:
        source_manager = local_manager_for(runtime, source_agent)
        target_manager = local_manager_for(runtime, target_agent)
    except AgentWorkspaceUnavailable as exc:
        # Never fall back to a guessed directory: report and change nothing.
        return await _reject(
            runtime, update, "sys_transfer_workspace_unresolved", str(exc)
        )
    entries = [
        SourceSlot(
            slot=str(item["slot"]),
            value=str(item.get("text") or ""),
            enabled=bool(item.get("active")),
        )
        for item in source_manager.list_slots()
        if str(item.get("text") or "").strip()
    ]
    if slot is not None and not any(entry.slot == slot for entry in entries):
        return await _reject(
            runtime,
            update,
            "sys_transfer_slot_missing",
            ui_language.tr(
                "sys.transfer.slot_missing", slot=slot, agent=source_agent
            ),
        )
    target_slots = [
        item
        for item in target_manager.list_slots()
        if str(item.get("text") or "").strip()
    ]
    plan = plan_slot_transfer(
        source_slots=entries,
        target_slot_ids=[str(item["slot"]) for item in target_slots],
        target_values=[str(item.get("text") or "") for item in target_slots],
        order=SysPromptManager.SLOTS,
        slot=slot,
    )
    applied: list[SlotTransfer] = []
    conflicts: list[str] = []
    for item in plan:
        if item.outcome != "copied":
            continue
        target_slot = str(item.target_slot)
        # Save and activate from the same observation: a slot filled by another
        # writer after the plan is reported instead of being overwritten.
        observed = dict(target_manager.get_slot(target_slot))
        try:
            target_manager.save(
                target_slot, str(item.value), expected_slot=observed
            )
        except SysPromptSemanticConflict:
            conflicts.append(str(item.source_slot))
            continue
        if item.enabled:
            try:
                target_manager.activate(
                    target_slot,
                    expected_slot={"text": str(item.value), "active": False},
                )
            except SysPromptSemanticConflict:
                conflicts.append(str(item.source_slot))
                continue
        applied.append(item)
    await _reply(
        runtime,
        update,
        _sys_transfer_receipt_text(
            verb=verb,
            source_agent=source_agent,
            target_agent=target_agent,
            plan=plan,
            applied=applied,
            conflicts=conflicts,
        ),
        parse_mode="HTML",
    )
    return None


def _usage() -> str:
    return ui_language.tr("sys.usage.full")


async def _reject(
    runtime: Any,
    update: Any,
    code: str,
    message: str,
) -> dict[str, Any]:
    await _reply(runtime, update, message)
    return {
        "ok": False,
        "error_code": code,
        "error_message": message,
        "request_outcome": "rejected",
    }


async def _mutate_or_reject(
    runtime: Any,
    update: Any,
    manager: SysPromptManager,
    *,
    scope: str,
    slot: str,
    action: str,
    actor_id: int | None,
    text: str | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    try:
        return (
            _mutate_slot(
                runtime,
                manager,
                scope=scope,
                slot=slot,
                action=action,
                actor_id=actor_id,
                source_channel="slash_command",
                text=text,
            ),
            None,
        )
    except SysPromptSemanticConflict as exc:
        if "occupied" in str(exc).casefold():
            message = ui_language.tr(
                "sys.error.global_already_configured"
                if scope == "global"
                else "sys.error.local_already_configured",
                slot=slot,
            )
            code = "sys_slot_occupied"
        else:
            message = ui_language.tr("sys.error.changed", slot=slot)
            code = "sys_slot_changed"
        return None, await _reject(runtime, update, code, message)
    except (ConfigConflictError, ConfigDurabilityError, OSError, TimeoutError):
        message = ui_language.tr("sys.error.persistence", slot=slot)
        return None, await _reject(
            runtime, update, "sys_persistence_failed", message
        )


async def cmd_sys(runtime: Any, update: Any, context: Any) -> dict[str, Any] | None:
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    if not _is_authorized(runtime, user_id):
        return
    message = getattr(update, "effective_message", None) or getattr(
        update, "message", None
    )
    command, parsed_args = parse_slash_command_text(
        str(getattr(message, "text", "") or "")
    )
    try:
        raw_args = normalize_sys_arguments(
            parsed_args
            if command == "sys"
            else (getattr(context, "args", None) or [])
        )
    except SysCommandContractError as exc:
        return await _reject(
            runtime, update, exc.error_code, exc.error_message
        )
    scope, args = _resolve_scope(raw_args)

    if args and args[0].lower() in _SYS_TRANSFER_VERBS:
        return await _cmd_sys_transfer(
            runtime, update, scope=scope, args=args, actor_id=user_id
        )

    manager = _manager_for_scope(runtime, scope)

    if not args:
        await _reply_slots(runtime, update, scope)
        return

    if args[0].lower() == "output":
        slot = args[1] if len(args) > 1 else ""
        if slot not in manager.SLOTS:
            return await _reject(
                runtime,
                update,
                "sys_slot_invalid",
                ui_language.tr(
                    "sys.usage.output",
                    prefix="/sys global" if scope == "global" else "/sys",
                ),
            )
        text = str(manager.get_slot(slot).get("text") or "")
        await _reply(
            runtime,
            update,
            text if text else ui_language.tr("common.empty"),
            parse_mode=None,
        )
        return

    slot = args[0]
    if slot not in manager.SLOTS:
        return await _reject(
            runtime,
            update,
            "sys_slot_invalid",
            ui_language.tr("sys.error.invalid_slot", slot=slot),
        )

    if len(args) == 1:
        await _reply_slot(runtime, update, scope, slot)
        return

    action = args[1].lower()
    confirmed = len(args) > 2 and args[2].upper() == "CONFIRM"

    if action == "on":
        if not manager.get_slot(slot).get("text"):
            return await _reject(
                runtime,
                update,
                "sys_slot_empty",
                ui_language.tr("sys.error.empty_slot", slot=slot),
            )
        if scope == "global" and not confirmed:
            await _reply_confirmation(runtime, update, scope, slot, "on")
            return
        notice, failure = await _mutate_or_reject(
            runtime,
            update,
            manager,
            scope=scope,
            slot=slot,
            action="on",
            actor_id=user_id,
        )
        if failure:
            return failure
        await _reply_slot(runtime, update, scope, slot, notice=notice)
        return

    if action == "off":
        notice, failure = await _mutate_or_reject(
            runtime,
            update,
            manager,
            scope=scope,
            slot=slot,
            action="off",
            actor_id=user_id,
        )
        if failure:
            return failure
        await _reply_slot(runtime, update, scope, slot, notice=notice)
        return

    if action == "delete":
        if scope == "global" and manager.get_slot(slot).get("text") and not confirmed:
            await _reply_confirmation(runtime, update, scope, slot, "delete")
            return
        notice, failure = await _mutate_or_reject(
            runtime,
            update,
            manager,
            scope=scope,
            slot=slot,
            action="delete",
            actor_id=user_id,
        )
        if failure:
            return failure
        await _reply_slot(runtime, update, scope, slot, notice=notice)
        return

    if action == "save":
        if manager.get_slot(slot).get("text"):
            return await _reject(
                runtime,
                update,
                "sys_slot_occupied",
                ui_language.tr(
                    "sys.error.global_already_configured"
                    if scope == "global"
                    else "sys.error.local_already_configured",
                    slot=slot,
                ),
            )
        text = args[2] if len(args) > 2 else ""
        if not text.strip():
            prefix = "/sys global" if scope == "global" else "/sys"
            return await _reject(
                runtime,
                update,
                "sys_prompt_required",
                ui_language.tr("sys.usage.save", prefix=prefix),
            )
        notice, failure = await _mutate_or_reject(
            runtime,
            update,
            manager,
            scope=scope,
            slot=slot,
            action="save",
            actor_id=user_id,
            text=text,
        )
        if failure:
            return failure
        await _reply_slot(runtime, update, scope, slot, notice=notice)
        return

    if action == "replace":
        text = args[2] if len(args) > 2 else ""
        is_active_global = scope == "global" and bool(
            manager.get_slot(slot).get("active")
        )
        confirmed_match = re.match(r"^CONFIRM(?:\r\n|[ \t\r\n])([\s\S]*)$", text)
        if is_active_global and confirmed_match is None:
            return await _reject(
                runtime,
                update,
                "sys_confirmation_required",
                ui_language.tr("sys.error.active_global_replace", slot=slot),
            )
        if is_active_global and confirmed_match is not None:
            text = confirmed_match.group(1)
        if not text.strip():
            prefix = "/sys global" if scope == "global" else "/sys"
            return await _reject(
                runtime,
                update,
                "sys_prompt_required",
                ui_language.tr("sys.usage.replace", prefix=prefix),
            )
        notice, failure = await _mutate_or_reject(
            runtime,
            update,
            manager,
            scope=scope,
            slot=slot,
            action="replace",
            actor_id=user_id,
            text=text,
        )
        if failure:
            return failure
        await _reply_slot(runtime, update, scope, slot, notice=notice)
        return

    return await _reject(
        runtime,
        update,
        "sys_action_invalid",
        ui_language.tr("sys.error.invalid_action"),
    )


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
        await query.answer(ui_language.tr("sys.error.disabled"), show_alert=True)
        return

    parts = str(getattr(query, "data", None) or "").split(":")
    if len(parts) not in {3, 4} or parts[0] != "sys":
        await query.answer(ui_language.tr("sys.error.invalid_action"), show_alert=True)
        return
    action = parts[1]
    scope = parts[2]
    slot = parts[3] if len(parts) == 4 else ""
    if scope not in {"local", "global"}:
        await query.answer(ui_language.tr("sys.error.invalid_scope"), show_alert=True)
        return
    manager = _manager_for_scope(runtime, scope)

    if action == "menu" and not slot:
        await _edit_slots(runtime, query, scope)
        await query.answer()
        return
    if slot not in manager.SLOTS:
        await query.answer(
            ui_language.tr("sys.error.invalid_callback_slot"), show_alert=True
        )
        return
    if action == "view":
        await _edit_slot(runtime, query, scope, slot)
    elif action == "output":
        text = str(manager.get_slot(slot).get("text") or "")
        await _reply_from_callback(
            runtime,
            update,
            query,
            text if text else ui_language.tr("common.empty"), parse_mode=None
        )
    elif action == "on":
        if not manager.get_slot(slot).get("text"):
            await query.answer(ui_language.tr("sys.error.save_first"), show_alert=True)
            return
        if scope == "global":
            await query.edit_message_text(
                confirm_card(
                    "⚠️",
                    "Activate global system prompt",
                    target=f"<code>global / {html.escape(slot)}</code>",
                    consequence=ui_language.tr(
                        "sys.confirm.activate_consequence",
                        instance=html.escape(_instance_id(runtime)),
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
                consequence=ui_language.tr(
                    "sys.confirm.delete_consequence_global"
                    if scope == "global"
                    else "sys.confirm.delete_consequence_local"
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
        await query.answer(ui_language.tr("sys.error.invalid_action"), show_alert=True)
        return
    await query.answer()
# HASHI-Fingerprint: hfp_f3504bb70f0f490886e6d172184a17dc
