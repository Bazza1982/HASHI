from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass

from telegram import BotCommand
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

from orchestrator.command_registry import bind_runtime_commands, runtime_bot_commands, runtime_command_map
from orchestrator.command_specs import COMMAND_SPECS
from orchestrator.private_wol import private_wol_available

logger = logging.getLogger("BridgeU.RuntimeCommandBinding")


@dataclass(frozen=True)
class CommandBinding:
    name: str
    method_name: str


@dataclass(frozen=True)
class CallbackBinding:
    pattern: str
    method_name: str


@dataclass(frozen=True)
class BotCommandBinding:
    name: str
    description: str


# Compatibility views. Handler registration and Telegram menu metadata are
# both derived from COMMAND_SPECS; neither is an independent fact source.
COMMAND_BINDINGS: tuple[CommandBinding, ...] = tuple(
    CommandBinding(spec.name, spec.method_name) for spec in COMMAND_SPECS
)
BOT_COMMAND_BINDINGS: tuple[BotCommandBinding, ...] = tuple(
    BotCommandBinding(spec.name, spec.description)
    for spec in COMMAND_SPECS
    if spec.menu_visible
)


CALLBACK_BINDINGS: tuple[CallbackBinding, ...] = (
    CallbackBinding(
        r"^(model|backend|bmodel|effort|backend_menu)",
        "callback_model",
    ),
    CallbackBinding(
        r"^(provider|provider_menu|provider_locked|pmodel)",
        "callback_claw_provider",
    ),
    CallbackBinding(r"^wcfg:", "callback_wrapper_config"),
    CallbackBinding(r"^acfg:", "callback_audit_config"),
    CallbackBinding(r"^bcfg:", "callback_brain_config"),
    CallbackBinding(r"^habit:", "callback_habit"),
    CallbackBinding(r"^npad:", "callback_notepad"),
    CallbackBinding(r"^privacy:", "callback_privacy"),
    CallbackBinding(r"^voice:", "callback_voice"),
    CallbackBinding(r"^safevoice:", "callback_safevoice"),
    CallbackBinding(r"^startagent:", "callback_start_agent"),
    CallbackBinding(r"^agents:", "callback_agents"),
    CallbackBinding(r"^(skill|skilljob|nudgejob):", "callback_skill"),
    CallbackBinding(r"^tgl:", "callback_toggle"),
    CallbackBinding(r"^group:", "callback_group"),
    CallbackBinding(r"^move:", "callback_move"),
)


def _split_runtime_command_text(text: str) -> tuple[str, list[str]]:
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return "", []
    try:
        parts = shlex.split(raw[1:])
    except Exception:
        parts = raw[1:].split()
    if not parts:
        return "", []
    command = parts[0].split("@", 1)[0].lower()
    return command, parts[1:]


async def _dispatch_dynamic_runtime_command(runtime, update, context) -> bool:
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    command_name, args = _split_runtime_command_text(getattr(message, "text", "") or "")
    if not command_name:
        return False
    command = runtime_command_map().get(command_name)
    if command is None:
        return False

    logger.info("Dispatching runtime command via dynamic fallback: /%s", command_name)
    setattr(context, "args", args)

    async def callback(inner_update, inner_context):
        await command.callback(runtime, inner_update, inner_context)

    handler = callback
    if hasattr(runtime, "_wrap_cmd"):
        handler = runtime._wrap_cmd(command_name, callback)
    await handler(update, context)
    return True


def bind_flexible_runtime_handlers(runtime) -> None:
    runtime.app.add_error_handler(runtime.handle_telegram_error)
    for binding in COMMAND_BINDINGS:
        callback = getattr(runtime, binding.method_name)
        runtime.app.add_handler(CommandHandler(binding.name, runtime._wrap_cmd(binding.name, callback)))
    for binding in CALLBACK_BINDINGS:
        callback = getattr(runtime, binding.method_name)
        if hasattr(runtime, "_wrap_callback"):
            callback = runtime._wrap_callback(binding.method_name, callback)
        runtime.app.add_handler(CallbackQueryHandler(callback, pattern=binding.pattern))

    bind_runtime_commands(runtime, wrap=True)

    async def dynamic_runtime_command_fallback(update, context):
        await _dispatch_dynamic_runtime_command(runtime, update, context)

    runtime.app.add_handler(MessageHandler(filters.COMMAND, dynamic_runtime_command_fallback))
    runtime.app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), runtime.handle_message))
    runtime.app.add_handler(MessageHandler(filters.PHOTO, runtime.handle_photo))
    runtime.app.add_handler(MessageHandler(filters.VOICE, runtime.handle_voice))
    runtime.app.add_handler(MessageHandler(filters.AUDIO, runtime.handle_audio))
    runtime.app.add_handler(MessageHandler(filters.Document.ALL, runtime.handle_document))
    runtime.app.add_handler(MessageHandler(filters.VIDEO, runtime.handle_video))
    runtime.app.add_handler(MessageHandler(filters.Sticker.ALL, runtime.handle_sticker))


def get_flexible_bot_commands(runtime) -> list[BotCommand]:
    commands = [BotCommand(binding.name, binding.description) for binding in BOT_COMMAND_BINDINGS]
    if private_wol_available(
        runtime.global_config.project_root,
        getattr(runtime.global_config, "instance_id", None),
    ):
        wol_spec = next(spec for spec in COMMAND_SPECS if spec.name == "wol")
        commands.append(BotCommand(wol_spec.name, wol_spec.description))
    return commands + runtime_bot_commands()
