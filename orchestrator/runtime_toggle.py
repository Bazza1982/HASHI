from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import runtime_mode
from orchestrator import runtime_reboot
from orchestrator.command_registry import RuntimeCallback


async def callback_toggle(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        await query.answer()
        return
    parts = (query.data or "").split(":", 2)
    if len(parts) < 3:
        await query.answer()
        return
    _, target, value = parts[0], parts[1], parts[2]

    if target == "verbose":
        runtime._verbose = value == "on"
        flag_path = runtime.workspace_dir / ".verbose_off"
        if runtime._verbose:
            flag_path.unlink(missing_ok=True)
        else:
            flag_path.touch()
        state = "ON 🔍" if runtime._verbose else "OFF"
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ ON" if runtime._verbose else "ON", callback_data="tgl:verbose:on"),
            InlineKeyboardButton("✅ OFF" if not runtime._verbose else "OFF", callback_data="tgl:verbose:off"),
        ]])
        await query.edit_message_text(f"Verbose mode: {state}", reply_markup=markup)
        await query.answer(f"Verbose {state}")
        return

    if target == "think":
        runtime._think = value == "on"
        flag_path = runtime.workspace_dir / ".think_off"
        if runtime._think:
            flag_path.unlink(missing_ok=True)
        else:
            flag_path.touch()
        state = "ON 💭" if runtime._think else "OFF"
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ ON" if runtime._think else "ON", callback_data="tgl:think:on"),
            InlineKeyboardButton("✅ OFF" if not runtime._think else "OFF", callback_data="tgl:think:off"),
        ]])
        await query.edit_message_text(f"Thinking display: {state}", reply_markup=markup)
        await query.answer(f"Think {state}")
        return

    if target == "mode":
        await runtime_mode.callback_mode_toggle(runtime, query, value)
        return

    if target == "retry":
        chat_id = query.message.chat_id
        await query.answer(f"Retrying {value}...")
        if value == "response":
            if runtime.last_response:
                await runtime.send_long_message(
                    chat_id=runtime.last_response["chat_id"],
                    text=runtime.last_response["text"],
                    request_id=runtime.last_response.get("request_id"),
                    purpose="retry-response",
                )
            else:
                transcript_text = runtime._load_last_text_from_transcript("assistant")
                if transcript_text:
                    await runtime.send_long_message(chat_id=chat_id, text=transcript_text, purpose="retry-response")
                elif runtime.last_prompt:
                    await runtime.enqueue_request(runtime.last_prompt.chat_id, runtime.last_prompt.prompt, "retry", "Retry request")
                else:
                    await query.answer("Nothing to retry.", show_alert=True)
        else:
            if runtime.last_prompt:
                await runtime.enqueue_request(runtime.last_prompt.chat_id, runtime.last_prompt.prompt, "retry", "Retry request")
            else:
                transcript_text = runtime._load_last_text_from_transcript("user")
                if transcript_text:
                    await runtime.enqueue_request(chat_id, transcript_text, "retry", "Retry request")
                else:
                    await query.answer("No previous prompt.", show_alert=True)
        return

    if target == "whisper":
        from orchestrator.voice_transcriber import get_transcriber

        mapping = {"small": "small", "medium": "medium", "large": "large-v3"}
        new_size = mapping.get(value)
        if not new_size:
            await query.answer("Unknown size.", show_alert=True)
            return
        transcriber = get_transcriber()
        transcriber.model_size = new_size
        transcriber._model = None
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ small" if value == "small" else "small", callback_data="tgl:whisper:small"),
            InlineKeyboardButton("✅ medium" if value == "medium" else "medium", callback_data="tgl:whisper:medium"),
            InlineKeyboardButton("✅ large" if value == "large" else "large", callback_data="tgl:whisper:large"),
        ]])
        await query.edit_message_text(f"Whisper model: <b>{new_size}</b>", parse_mode="HTML", reply_markup=markup)
        await query.answer(f"Set to {new_size}")
        return

    if target == "active":
        if not runtime.skill_manager:
            await query.answer("Skill manager not available.", show_alert=True)
            return
        if value == "off":
            _, msg = runtime.skill_manager.set_active_heartbeat(runtime.name, enabled=False)
        elif value == "on":
            _, msg = runtime.skill_manager.set_active_heartbeat(
                runtime.name,
                enabled=True,
                minutes=runtime.skill_manager.ACTIVE_HEARTBEAT_DEFAULT_MINUTES,
            )
        else:
            try:
                mins = int(value)
                _, msg = runtime.skill_manager.set_active_heartbeat(runtime.name, enabled=True, minutes=mins)
            except ValueError:
                await query.answer("Invalid value.", show_alert=True)
                return
        status = runtime.skill_manager.describe_active_heartbeat(runtime.name)
        markup = runtime._active_keyboard()
        await query.edit_message_text(f"{status}\n\n{msg}", reply_markup=markup)
        await query.answer()
        return

    if target == "reboot":
        await runtime_reboot.callback_reboot_toggle(runtime, query, value)
        return

    await query.answer()


CALLBACKS = [
    RuntimeCallback(pattern=r"^tgl:", callback=callback_toggle),
]
