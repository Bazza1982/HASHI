"""Native /getfile command and opaque, origin-bound callbacks (no Engine Run)."""
from __future__ import annotations

import asyncio
import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from orchestrator import ui_language
from orchestrator.command_registry import RuntimeCommand, RuntimeCallback
from orchestrator.command_ui import card_title
from orchestrator.getfile_service import GetFileError, GetFileService


def tr(key, **values):
    return ui_language.tr("getfile." + key, **values)


async def _send(runtime, update, text, **kwargs):
    update._hashi_origin_only = True
    return await runtime._reply_text(update, text, parse_mode="HTML", **kwargs)


async def _menu(runtime, update, *, query=None):
    service = GetFileService(runtime)
    token, items = await asyncio.to_thread(service.menu, update)
    lines = [card_title("📎", "GETFILE"), tr("coverage")]
    rows = []
    for number, item in enumerate(items, 1):
        name = item["relative_path"].rsplit("/", 1)[-1]
        label = (f"{number}. " + name)[:64]
        info = tr(item["action"]) + " · " + item["observed_at"][:16].replace("T", " ")
        if item.get("evidence") != "verified":
            info += " · " + tr("reported")
        lines.append(f'{number}. <b>{html.escape(name[:100])}</b> · {html.escape(info)}')
        lines.append("<code>" + html.escape(item["relative_path"][-120:]) + "</code>")
        api = {}
        if item.get("error"):
            label = "🔒 " + label
            lines.append(html.escape(tr("error." + item["error"])))
            api["hashi_disabled_reason"] = tr("error." + item["error"])
        rows.append([InlineKeyboardButton(label, callback_data=f"getfile:file:{token}:{number-1}", api_kwargs=api)])
    if not items:
        lines.append(tr("empty"))
    rows.append([InlineKeyboardButton(tr("refresh"), callback_data="getfile:refresh")])
    if str(getattr(update, "_hashi_session_surface", "") or "") != "telegram":
        rows.append([InlineKeyboardButton(tr("path_send"), callback_data=f"getfile:path:{token}",
            api_kwargs={"hashi_text_input": {"label": tr("path_label"), "value": "", "max_length": 500}})])
    lines.extend(["", tr("limits"), tr("usage")])
    markup = InlineKeyboardMarkup(rows)
    if query is not None:
        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=markup)
    else:
        await _send(runtime, update, "\n".join(lines), reply_markup=markup)


def _raw_path(update, context):
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    text = str(getattr(message, "text", "") or "")
    if text.lstrip().startswith("/getfile"):
        parts = text.strip().split(maxsplit=1)
        return parts[1] if len(parts) == 2 else ""
    return " ".join(getattr(context, "args", None) or []).strip()


async def _finish(runtime, update, service, **selection):
    result = await service.transfer(update, **selection)
    state = result["getfile"]["delivery_state"]
    key = "delivered" if state in {"delivered", "accepted"} else "published" if state == "published" else "queued" if state == "queued" else "uncertain"
    await _send(runtime, update, html.escape(tr(key)))
    return result


async def getfile_command(runtime, update, context):
    try:
        service = GetFileService(runtime)
        service.bind(update)
        path = _raw_path(update, context)
        if path:
            return await _finish(runtime, update, service, raw_path=path)
        await _menu(runtime, update)
    except GetFileError as exc:
        await _send(runtime, update, html.escape(tr("error." + exc.code)))
    except (OSError, ValueError):
        await _send(runtime, update, html.escape(tr("error.unavailable")))


async def getfile_callback(runtime, update, context):
    query = update.callback_query
    update._hashi_origin_only = True
    try:
        service = GetFileService(runtime)
        service.bind(update)
        parts = str(query.data).split(":")
        await query.answer()
        if parts == ["getfile", "refresh"]:
            return await _menu(runtime, update, query=query)
        if len(parts) == 4 and parts[1] == "file":
            # Each transfer key survives callback retries, process replacement and hot reboot.
            result = await _finish(runtime, update, service, token=parts[2], index=int(parts[3]))
        elif len(parts) == 3 and parts[1] == "path":
            path = getattr(query, "input_text", None)
            if not isinstance(path, str) or not 1 <= len(path) <= 500:
                raise GetFileError("invalid_path")
            result = await _finish(runtime, update, service, token=parts[2], raw_path=path)
        else:
            raise GetFileError("invalid_path")
        # Preserve refresh and status after one action; double clicks remain bound
        # to the same publication rather than generating another file.
        await query.edit_message_text(html.escape(tr("complete")), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(tr("refresh"), callback_data="getfile:refresh")]]))
        return result
    except GetFileError as exc:
        await _send(runtime, update, html.escape(tr("error." + exc.code)))
    except (OSError, ValueError):
        await _send(runtime, update, html.escape(tr("error.unavailable")))


def get_commands():
    return [RuntimeCommand("getfile", "Get recent or named files", getfile_command)]


def get_callbacks():
    return [RuntimeCallback(r"^getfile:", getfile_callback)]
