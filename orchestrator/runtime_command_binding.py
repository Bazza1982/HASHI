from __future__ import annotations

from dataclasses import dataclass

from telegram import BotCommand
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

from orchestrator.command_registry import bind_runtime_commands, runtime_bot_commands


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


COMMAND_BINDINGS: tuple[CommandBinding, ...] = ()


BOT_COMMAND_BINDINGS: tuple[BotCommandBinding, ...] = (
)


CALLBACK_BINDINGS: tuple[CallbackBinding, ...] = ()


def bind_flexible_runtime_handlers(runtime) -> None:
    runtime.app.add_error_handler(runtime.handle_telegram_error)
    for binding in COMMAND_BINDINGS:
        callback = getattr(runtime, binding.method_name)
        runtime.app.add_handler(CommandHandler(binding.name, runtime._wrap_cmd(binding.name, callback)))
    for binding in CALLBACK_BINDINGS:
        runtime.app.add_handler(CallbackQueryHandler(getattr(runtime, binding.method_name), pattern=binding.pattern))

    bind_runtime_commands(runtime, wrap=True)

    runtime.app.add_handler(MessageHandler(filters.COMMAND, runtime.handle_workspace_command))
    runtime.app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), runtime.handle_message))
    runtime.app.add_handler(MessageHandler(filters.PHOTO, runtime.handle_photo))
    runtime.app.add_handler(MessageHandler(filters.VOICE, runtime.handle_voice))
    runtime.app.add_handler(MessageHandler(filters.AUDIO, runtime.handle_audio))
    runtime.app.add_handler(MessageHandler(filters.Document.ALL, runtime.handle_document))
    runtime.app.add_handler(MessageHandler(filters.VIDEO, runtime.handle_video))
    runtime.app.add_handler(MessageHandler(filters.Sticker.ALL, runtime.handle_sticker))


def get_flexible_bot_commands(runtime) -> list[BotCommand]:
    commands = [BotCommand(binding.name, binding.description) for binding in BOT_COMMAND_BINDINGS]
    return commands + runtime_bot_commands()
