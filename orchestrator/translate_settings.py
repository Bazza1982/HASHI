"""Independent /translate renderer and settings actions."""
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import ui_language
from orchestrator.command_ui import refresh_label, selected_label, setting_card
from orchestrator.phone_manager import PhoneConfigError


def render(runtime):
    manager = runtime.translate_manager
    state = manager.get_state()
    model = manager.PROVIDERS[state["provider"]]["models"][state["model"]]
    direction = model["direction"]
    text = setting_card(
        "🌐", ui_language.tr("translate.title"),
        current=escape(ui_language.tr("translate.direction." + direction)),
        facts=(escape(ui_language.tr("translate.privacy")),
               escape(ui_language.tr("translate.audio_scope"))),
        consequence=ui_language.tr("translate.effect"),
        action=ui_language.tr("translate.action"),
    )
    rows = [[InlineKeyboardButton(
        selected_label(ui_language.tr("translate.direction." + key), key == direction),
        callback_data="translate:direction:" + key,
    ) for key, _label in manager.direction_options()]]
    rows.append([InlineKeyboardButton(refresh_label(), callback_data="translate:refresh")])
    return text, InlineKeyboardMarkup(rows)


async def command(runtime, update, context):
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    args = [str(value).strip() for value in (context.args or []) if str(value).strip()]
    action = args[0].casefold() if args else "status"
    try:
        if action == "direction" and len(args) == 2:
            runtime.translate_manager.set_direction(args[1])
        elif action == "reset" and len(args) == 1:
            runtime.translate_manager.reset()
        elif action not in {"status", "menu", "direction"} or len(args) > 1:
            await runtime._reply_text(update, ui_language.tr("translate.usage"))
            return
        text, keyboard = render(runtime)
    except (PhoneConfigError, ValueError) as exc:
        await runtime._reply_text(update, ui_language.tr("translate.error").format(error=escape(str(exc))))
        return
    await runtime._reply_text(update, text, reply_markup=keyboard, parse_mode="HTML")


async def callback(runtime, update, context):
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        return
    parts = str(query.data or "").split(":")
    try:
        if len(parts) == 3 and parts[1] == "direction":
            runtime.translate_manager.set_direction(parts[2])
        elif parts != ["translate", "refresh"]:
            raise ValueError(ui_language.tr("translate.usage"))
        text, keyboard = render(runtime)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
    except (PhoneConfigError, ValueError) as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await query.answer()
