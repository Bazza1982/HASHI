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

from orchestrator import ui_language

logger = logging.getLogger("BridgeU.CommandRegistry")
DEFAULT_PRIVATE_COMMAND_DIR = Path.home() / ".hashi" / "private_commands"
NON_OVERRIDABLE_CORE_COMMANDS = frozenset({"queue", "wiki"})

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


@dataclass(frozen=True)
class RuntimeRegistrySnapshot:
    commands: tuple[RuntimeCommand, ...]
    callbacks: tuple[RuntimeCallback, ...]


_registry_snapshot: RuntimeRegistrySnapshot | None = None
_registry_source_signature: tuple[Any, ...] | None = None


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
    module.__hashi_private_command_path__ = str(path)
    sys.modules[module_name] = module
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(str(path.parent))
    return module


def _is_private_command_module(module: Any) -> bool:
    return bool(getattr(module, "__hashi_private_command_path__", None))


def _module_label(module: Any) -> str:
    private_path = getattr(module, "__hashi_private_command_path__", None)
    if private_path:
        return Path(private_path).name
    return str(getattr(module, "__name__", "unknown"))


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


def _source_signature() -> tuple[Any, ...]:
    public_modules = tuple(_iter_command_modules())
    private_files: list[tuple[str, int | None, int | None]] = []
    for path in _iter_private_command_files():
        try:
            stat = path.stat()
            private_files.append((str(path.resolve()), stat.st_mtime_ns, stat.st_size))
        except OSError:
            private_files.append((str(path), None, None))
    return public_modules, tuple(private_files)


def invalidate_runtime_registry_cache() -> None:
    """Force the next registry read to rebuild its command snapshot."""

    global _registry_snapshot, _registry_source_signature
    _registry_snapshot = None
    _registry_source_signature = None


def _build_runtime_registry_snapshot() -> RuntimeRegistrySnapshot:
    commands: dict[str, RuntimeCommand] = {}
    callbacks: list[RuntimeCallback] = []
    for module in _iter_runtime_modules():
        module_commands = list(_commands_from_module(module))
        is_private = _is_private_command_module(module)
        protected = {
            command.name
            for command in module_commands
            if is_private and command.name in NON_OVERRIDABLE_CORE_COMMANDS
        }
        for command in module_commands:
            if command.name in commands:
                if is_private:
                    if command.name in NON_OVERRIDABLE_CORE_COMMANDS:
                        logger.warning(
                            "Ignoring private override of protected core command %s from %s",
                            command.name,
                            _module_label(module),
                        )
                        continue
                    logger.debug(
                        "Runtime command %s intentionally overridden by private command module %s",
                        command.name,
                        _module_label(module),
                    )
                else:
                    logger.warning(
                        "Runtime command %s overwritten by %s",
                        command.name,
                        _module_label(module),
                    )
            commands[command.name] = command
        module_callbacks = list(_callbacks_from_module(module))
        if protected and module_callbacks:
            logger.warning(
                "Ignoring callbacks from private override of protected core command(s) %s in %s",
                ", ".join(sorted(protected)),
                _module_label(module),
            )
            continue
        callbacks.extend(module_callbacks)
    return RuntimeRegistrySnapshot(
        commands=tuple(commands[name] for name in sorted(commands)),
        callbacks=tuple(callbacks),
    )


def _runtime_registry_snapshot() -> RuntimeRegistrySnapshot:
    global _registry_snapshot, _registry_source_signature
    signature = _source_signature()
    if _registry_snapshot is None or signature != _registry_source_signature:
        _registry_snapshot = _build_runtime_registry_snapshot()
        _registry_source_signature = signature
    return _registry_snapshot


def load_runtime_commands() -> list[RuntimeCommand]:
    return list(_runtime_registry_snapshot().commands)


def load_runtime_callbacks() -> list[RuntimeCallback]:
    return list(_runtime_registry_snapshot().callbacks)


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
            with ui_language.language_scope(runtime, update):
                await _callback.callback(runtime, update, context)

        runtime.app.add_handler(CallbackQueryHandler(handler, pattern=callback.pattern))


def runtime_bot_commands() -> list[BotCommand]:
    return [BotCommand(command.name, command.description) for command in load_runtime_commands()]
