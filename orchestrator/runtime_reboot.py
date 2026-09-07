"""One Frontend command path for acknowledged reboot requests and status."""

from __future__ import annotations

from html import escape
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import ui_language
from orchestrator.command_ui import card_title, refresh_label
from orchestrator.reboot_ui import render_status


def origin_from_update(runtime, update):
    message = getattr(update, "effective_message", None)
    query = getattr(update, "callback_query", None)
    if message is None and query is not None:
        message = getattr(query, "message", None)
    return {
        "surface": getattr(update, "_hashi_session_surface", None) or "telegram",
        "actor_id": ui_language.actor_id_from_update(update),
        "chat_id": ui_language.chat_id_from_update(update),
        "thread_id": getattr(message, "message_thread_id", None),
    }


async def latest(runtime, update):
    origin = origin_from_update(runtime, update)
    return await runtime.orchestrator.reboot_status(**origin)


async def submit(runtime, update, *, mode, number=None, targets=None, query=None):
    origin = origin_from_update(runtime, update)
    request_key = str(
        getattr(update, "update_id", None) or getattr(query, "id", None) or uuid4().hex
    )
    request = {
        "mode": mode,
        "agent_name": runtime.name,
        "agent_number": number,
        "origin": origin,
        "locale": ui_language.preferred_locale(runtime, update),
        "request_key": request_key,
    }
    if targets is not None:
        request["targets"] = list(targets)
    try:
        result = await runtime.orchestrator.request_reboot(**request)
    except Exception:
        # A lost acknowledgement cannot establish that the request was rejected.
        text = ui_language.tr("reboot.ack_unknown")
    else:
        if result.get("duplicate"):
            text = render_status(
                result["record"], locale=ui_language.preferred_locale(runtime, update)
            )
        elif result["accepted"]:
            if origin["surface"] == "telegram":
                return result  # Shared runtime sends start and final notices.
            text = ui_language.tr("reboot.accepted")
        else:
            text = ui_language.tr(
                "reboot.not_accepted",
                reason=ui_language.tr("reboot.reason." + result["reason"]),
            )
    if query is not None:
        await query.edit_message_text(text, parse_mode="HTML")
    else:
        await runtime._reply_text(update, text, parse_mode="HTML")
    return None


async def show_menu(runtime, update, *, status_only=False, query=None):
    orchestrator = runtime.orchestrator
    try:
        record = await latest(runtime, update)
        status = render_status(
            record, locale=ui_language.preferred_locale(runtime, update)
        )
    except Exception:
        status = ui_language.tr("reboot.status_unavailable")
    lines = [card_title("🔄", "Reboot agents"), "", status]
    rows = [[InlineKeyboardButton(refresh_label(), callback_data="tgl:reboot:status")]]
    if not status_only:
        running = {rt.name: rt for rt in orchestrator.runtimes}
        lines.extend(
            [
                "",
                escape(ui_language.tr("reboot.current", count=len(running))),
                escape(ui_language.tr("reboot.effect")),
                "",
                escape(ui_language.tr("reboot.warning")),
            ]
        )
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        ui_language.tr("reboot.this_agent"),
                        callback_data="tgl:reboot:min",
                    ),
                    InlineKeyboardButton(
                        ui_language.tr("reboot.all_active"),
                        callback_data="tgl:reboot:max",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        ui_language.tr("reboot.all_running"),
                        callback_data="tgl:reboot:same",
                    )
                ],
            ]
        )
        for i, name in enumerate(orchestrator.configured_agent_names(), 1):
            rt = running.get(name)
            display = rt.get_display_name() if rt is not None else name
            lines.append(f"{'●' if rt else '○'} {i}. {escape(display)}")
            rows.append(
                [
                    InlineKeyboardButton(
                        f"#{i} {display}", callback_data=f"tgl:reboot:{i}"
                    )
                ]
            )
    kwargs = {"parse_mode": "HTML", "reply_markup": InlineKeyboardMarkup(rows)}
    if query is not None:
        await query.edit_message_text("\n".join(lines), **kwargs)
    else:
        await runtime._reply_text(update, "\n".join(lines), **kwargs)


async def choose(runtime, update, value, *, query=None):
    if value in {"", "help", "status"}:
        await show_menu(runtime, update, status_only=value == "status", query=query)
        return
    if value in {"min", "same", "max"}:
        await submit(runtime, update, mode=value, query=query)
    elif value.isdigit():
        await submit(runtime, update, mode="number", number=int(value), query=query)
    else:
        text = ui_language.tr("reboot.invalid_target")
        if query is not None:
            await query.edit_message_text(text)
        else:
            await runtime._reply_text(update, text)


async def command(runtime, update, context):
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if getattr(runtime, "orchestrator", None) is None:
        await runtime._reply_text(update, ui_language.tr("reboot.unavailable"))
        return
    await choose(runtime, update, " ".join(context.args or []).strip().lower())


async def callback(runtime, update, query, value):
    await query.answer()
    await choose(runtime, update, value, query=query)
