from __future__ import annotations

import importlib
import inspect
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("ExtensionCommandRegistry")

COMMAND_CONFIG = "workspace_commands.json"


@dataclass(frozen=True)
class WorkspaceCommandSpec:
    name: str
    description: str
    factory: str
    enabled: bool = True
    options: dict[str, Any] | None = None


def load_workspace_command_specs(workspace_dir: Path) -> list[WorkspaceCommandSpec]:
    path = workspace_dir / COMMAND_CONFIG
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return []
    if isinstance(raw, dict):
        raw = raw.get("commands", [])
    if not isinstance(raw, list):
        logger.warning("%s must contain a list or an object with a commands list", path)
        return []

    specs: list[WorkspaceCommandSpec] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().lstrip("/").lower()
        factory = str(item.get("factory") or "").strip()
        if not name or not factory:
            continue
        specs.append(
            WorkspaceCommandSpec(
                name=name,
                description=str(item.get("description") or ""),
                factory=factory,
                enabled=bool(item.get("enabled", True)),
                options=dict(item.get("options") or {}),
            )
        )
    return specs


def available_workspace_commands(workspace_dir: Path) -> list[WorkspaceCommandSpec]:
    return [spec for spec in load_workspace_command_specs(workspace_dir) if spec.enabled]


def get_workspace_command_spec(workspace_dir: Path, command_name: str) -> WorkspaceCommandSpec | None:
    command_name = (command_name or "").strip().lstrip("/").lower()
    for spec in available_workspace_commands(workspace_dir):
        if spec.name == command_name:
            return spec
    return None


async def execute_workspace_command(
    runtime: Any,
    command_name: str,
    args: list[str],
    *,
    update: Any | None = None,
    context: Any | None = None,
) -> str:
    spec = get_workspace_command_spec(runtime.workspace_dir, command_name)
    if spec is None:
        raise KeyError(command_name)

    factory = _load_factory(spec.factory)
    handler = factory(runtime=runtime, options=dict(spec.options or {}))
    execute = getattr(handler, "execute", handler)
    result = execute(args=list(args or []), update=update, context=context)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, dict):
        if result.get("ok") is False:
            return str(result.get("error") or "Command failed.")
        return str(result.get("text") or "")
    return str(result or "")


def _load_factory(factory_path: str):
    module_name, sep, attr_name = factory_path.partition(":")
    if not sep:
        module_name, sep, attr_name = factory_path.rpartition(".")
    if not module_name or not attr_name:
        raise ValueError(f"Invalid command factory path: {factory_path!r}")
    module = importlib.import_module(module_name)
    factory = getattr(module, attr_name)
    if not callable(factory):
        raise TypeError(f"Command factory is not callable: {factory_path}")
    return factory
