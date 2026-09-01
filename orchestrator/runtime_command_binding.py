from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass

from telegram import BotCommand, BotCommandScopeChat
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

from orchestrator.command_registry import bind_runtime_commands, runtime_bot_commands, runtime_command_map
from orchestrator.command_specs import COMMAND_SPECS
from orchestrator.private_wol import private_wol_available
from orchestrator import ui_language

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
        r"^(model|backend|bmodel|effort|backend_menu|her_model|her_route|her_routes|her_reasoning|her_target)",
        "callback_model",
    ),
    CallbackBinding(
        r"^her_provider",
        "callback_model",
    ),
    CallbackBinding(r"^(wcfg|acfg|bcfg):", "callback_retired_agent_mode"),
    CallbackBinding(r"^habit:", "callback_habit"),
    CallbackBinding(r"^dream:", "callback_dream"),
    CallbackBinding(r"^npad:", "callback_notepad"),
    CallbackBinding(r"^privacy:", "callback_privacy"),
    CallbackBinding(r"^voice:", "callback_voice"),
    CallbackBinding(r"^safevoice:", "callback_safevoice"),
    CallbackBinding(r"^sys:", "callback_sys"),
    CallbackBinding(r"^wz:", "callback_workzone"),
    CallbackBinding(r"^startagent:", "callback_start_agent"),
    CallbackBinding(r"^agents:", "callback_agents"),
    CallbackBinding(r"^(skill|skilljob|nudgejob):", "callback_skill"),
    CallbackBinding(r"^tgl:", "callback_toggle"),
    CallbackBinding(r"^group:", "callback_group"),
    CallbackBinding(r"^move:", "callback_move"),
    CallbackBinding(r"^language:", "callback_language"),
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


def get_flexible_bot_commands(runtime, *, locale: str | None = None) -> list[BotCommand]:
    selected = ui_language.normalize_locale(locale or ui_language.DEFAULT_LOCALE)
    commands = [
        BotCommand(
            binding.name,
            ui_language.command_description(
                binding.name,
                binding.description,
                locale=selected,
            ),
        )
        for binding in BOT_COMMAND_BINDINGS
    ]
    if private_wol_available(
        runtime.global_config.project_root,
        getattr(runtime.global_config, "instance_id", None),
    ):
        wol_spec = next(spec for spec in COMMAND_SPECS if spec.name == "wol")
        commands.append(
            BotCommand(
                wol_spec.name,
                ui_language.command_description(
                    wol_spec.name,
                    wol_spec.description,
                    locale=selected,
                ),
            )
        )
    dynamic = [
        BotCommand(
            command.command,
            ui_language.command_description(
                command.command,
                command.description,
                locale=selected,
            ),
        )
        for command in runtime_bot_commands()
    ]
    return commands + dynamic


async def register_flexible_bot_commands(runtime) -> None:
    """Register the instance default and any saved per-user command menus."""

    default_locale = ui_language.configured_default_locale(runtime)
    await runtime.app.bot.set_my_commands(
        get_flexible_bot_commands(runtime, locale=default_locale)
    )
    for actor_id, locale in ui_language.saved_user_locales(runtime).items():
        try:
            chat_id = int(actor_id)
            if chat_id <= 0:
                continue
            await runtime.app.bot.set_my_commands(
                get_flexible_bot_commands(runtime, locale=locale),
                scope=BotCommandScopeChat(chat_id=chat_id),
            )
        except (TypeError, ValueError):
            continue
        except Exception as exc:
            logger.warning(
                "Could not restore chat command menu for %s on %s: %s",
                actor_id,
                getattr(runtime, "name", "unknown"),
                exc,
            )


async def sync_user_command_menus(
    runtime,
    *,
    chat_id: int | str,
    locale: str,
) -> int:
    """Refresh one user's private-chat command menu on every live agent."""

    try:
        numeric_chat_id = int(chat_id)
    except (TypeError, ValueError):
        return 0
    if numeric_chat_id <= 0:
        return 0

    orchestrator = getattr(runtime, "orchestrator", None)
    candidates = list(getattr(orchestrator, "runtimes", ()) or ())
    if not candidates:
        candidates = [runtime]
    failures = 0
    for candidate in candidates:
        bot = getattr(getattr(candidate, "app", None), "bot", None)
        if bot is None or not getattr(candidate, "telegram_connected", True):
            continue
        try:
            await bot.set_my_commands(
                get_flexible_bot_commands(candidate, locale=locale),
                scope=BotCommandScopeChat(chat_id=numeric_chat_id),
            )
        except Exception as exc:
            failures += 1
            logger.warning(
                "Could not refresh %s command menu for chat %s on %s: %s",
                locale,
                numeric_chat_id,
                getattr(candidate, "name", "unknown"),
                exc,
            )
    return failures
