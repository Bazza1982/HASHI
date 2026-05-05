from __future__ import annotations

import contextlib
import importlib
import importlib.util
import logging
import os
import pkgutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from telegram import BotCommand
from telegram.ext import CallbackQueryHandler, CommandHandler

logger = logging.getLogger("BridgeU.CommandRegistry")
DEFAULT_PRIVATE_COMMAND_DIR = Path.home() / ".hashi" / "private_commands"

CommandCallback = Callable[[Any, Any, Any], Awaitable[None]]


@dataclass(frozen=True)
class RuntimeCommand:
    name: str
    description: str
    callback: CommandCallback


@dataclass(frozen=True)
class RuntimeCallback:
    pattern: str
    callback: CommandCallback


def _iter_command_modules() -> Iterable[str]:
    try:
        package = importlib.import_module("orchestrator.commands")
    except ModuleNotFoundError:
        return
    for module_info in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
        if not module_info.ispkg:
            yield module_info.name


def _iter_private_command_files() -> Iterable[Path]:
    raw_dirs = os.environ.get("HASHI_PRIVATE_COMMAND_DIRS", "")
    directories = [DEFAULT_PRIVATE_COMMAND_DIR]
    directories.extend(Path(part).expanduser() for part in raw_dirs.split(os.pathsep) if part.strip())
    seen: set[Path] = set()
    for directory in directories:
        try:
            resolved = directory.resolve()
        except Exception:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        for path in sorted(resolved.glob("*.py")):
            if not path.name.startswith("_"):
                yield path


def _load_private_command_module(path: Path):
    module_name = f"_hashi_private_command_{path.stem}_{abs(hash(path.resolve()))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load private command module: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(str(path.parent))
    return module


def _commands_from_module(module) -> Iterable[RuntimeCommand]:
    get_commands = getattr(module, "get_commands", None)
    commands = get_commands() if callable(get_commands) else getattr(module, "COMMANDS", [])
    for command in commands or []:
        if isinstance(command, RuntimeCommand):
            yield command
        else:
            logger.warning("Ignoring invalid runtime command from %s: %r", module.__name__, command)


def _callbacks_from_module(module) -> Iterable[RuntimeCallback]:
    get_callbacks = getattr(module, "get_callbacks", None)
    callbacks = get_callbacks() if callable(get_callbacks) else getattr(module, "CALLBACKS", [])
    for callback in callbacks or []:
        if isinstance(callback, RuntimeCallback):
            yield callback
        else:
            logger.warning("Ignoring invalid runtime callback from %s: %r", module.__name__, callback)


def _iter_runtime_modules():
    for module_name in _iter_command_modules():
        try:
            yield importlib.import_module(module_name)
        except Exception as exc:
            logger.warning("Failed to import command module %s: %s", module_name, exc)
    for path in _iter_private_command_files():
        try:
            yield _load_private_command_module(path)
        except Exception as exc:
            logger.warning("Failed to import private command module %s: %s", path.name, exc)


def load_runtime_commands() -> list[RuntimeCommand]:
    commands: dict[str, RuntimeCommand] = {}
    for module in _iter_runtime_modules():
        for command in _commands_from_module(module):
            if command.name in commands:
                logger.warning("Runtime command %s overwritten by %s", command.name, module.__name__)
            commands[command.name] = command
    return [commands[name] for name in sorted(commands)]


def load_runtime_callbacks() -> list[RuntimeCallback]:
    callbacks: list[RuntimeCallback] = []
    for module in _iter_runtime_modules():
        callbacks.extend(_callbacks_from_module(module))
    return callbacks


def runtime_command_map() -> dict[str, RuntimeCommand]:
    return {command.name: command for command in load_runtime_commands()}


def bind_runtime_commands(runtime, *, wrap: bool = False) -> None:
    for command in load_runtime_commands():
        async def handler(update, context, _command=command):
            await _command.callback(runtime, update, context)

        if wrap and hasattr(runtime, "_wrap_cmd"):
            handler = runtime._wrap_cmd(command.name, handler)
        runtime.app.add_handler(CommandHandler(command.name, handler))
    for callback in load_runtime_callbacks():
        async def handler(update, context, _callback=callback):
            await _callback.callback(runtime, update, context)

        runtime.app.add_handler(CallbackQueryHandler(handler, pattern=callback.pattern))


def runtime_bot_commands() -> list[BotCommand]:
    return [BotCommand(command.name, command.description) for command in load_runtime_commands()]
