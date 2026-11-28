from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import Any

from orchestrator.agent_config_json import write_agent_config_json
from orchestrator.config_json import new_config_json, read_managed_json
from orchestrator.function_contract import is_function_module_name
from orchestrator.post_turn_observer import PostTurnObserver

logger = logging.getLogger("PostTurnRegistry")

OBSERVER_CONFIG = "post_turn_observers.json"
MEMORY_PLUS_OBSERVER_ID = "memory_plus"

# The release owns the implementations it ships. Agent config only selects them.
BUNDLED_OBSERVERS = {
    MEMORY_PLUS_OBSERVER_ID: (
        "orchestrator.memory_plus_mode", "build_memory_plus_observer",
    ),
}
_RETIRED_OBSERVERS = {
    "anatta": ("orchestrator.anatta.post_turn_observer", "build_post_turn_observer"),
}


def build_post_turn_observers(
    *,
    workspace_dir: Path,
    bridge_memory_store: Any,
    backend_invoker: Any | None = None,
    backend_context_getter: Any | None = None,
) -> list[PostTurnObserver]:
    observers: list[PostTurnObserver] = []
    for item in _read_observer_entries(workspace_dir / OBSERVER_CONFIG):
        spec = {"factory": item} if isinstance(item, str) else item
        if not isinstance(spec, dict) or not spec.get("enabled", True):
            continue
        try:
            module_name, attribute = _resolve_factory(spec)
            if not is_function_module_name(module_name):
                raise ValueError(f"Observer is outside project Functions: {module_name}")
            factory = getattr(importlib.import_module(module_name), attribute)
            if not callable(factory):
                raise TypeError(f"Observer factory is not callable: {module_name}:{attribute}")
            observer = factory(
                workspace_dir=workspace_dir,
                bridge_memory_store=bridge_memory_store,
                backend_invoker=backend_invoker,
                backend_context_getter=backend_context_getter,
                options=dict(spec.get("options") or {}),
            )
            if observer is not None:
                observers.append(observer)
        except Exception as exc:
            logger.warning("Skipping post-turn observer in %s: %s", workspace_dir, exc)
    return observers


def _read_observer_entries(path: Path) -> list[Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return []
    if isinstance(raw, dict):
        raw = raw.get("observers", [])
    if not isinstance(raw, list):
        logger.warning("%s must contain a list or an object with an observers list", path)
        return []
    return raw


def _factory_parts(factory_path: str) -> tuple[str, str]:
    module_name, sep, attr_name = factory_path.partition(":")
    if not sep:
        module_name, sep, attr_name = factory_path.rpartition(".")
    if not module_name or not attr_name:
        raise ValueError(f"Invalid observer factory path: {factory_path!r}")
    return module_name, attr_name


def _resolve_factory(spec: dict[str, Any]) -> tuple[str, str]:
    observer_id = str(spec.get("observer_id") or "").strip()
    if observer_id:
        if observer_id in _RETIRED_OBSERVERS:
            raise ValueError(f"Observer {observer_id!r} was retired")
        if observer_id not in BUNDLED_OBSERVERS:
            raise ValueError(f"Unknown observer ID: {observer_id!r}")
        return BUNDLED_OBSERVERS[observer_id]

    factory = _factory_parts(str(spec.get("factory") or "").strip())
    if factory in _RETIRED_OBSERVERS.values():
        raise ValueError(f"Observer {':'.join(factory)} was retired")
    # Legacy project factories can resolve from the qualified artifact.
    return factory


def ensure_bundled_observer(workspace_dir: Path, observer_id: str) -> bool:
    # Keep the legacy spelling on new records so the previous version can read
    # additions made after an upgrade. The current version resolves by ID.
    factory = ":".join(BUNDLED_OBSERVERS[observer_id])
    legacy_spellings = {factory, factory.replace(":", ".")}
    path = workspace_dir / OBSERVER_CONFIG
    try:
        config = read_managed_json(path)
    except FileNotFoundError:
        config = new_config_json(path)
    raw = config if isinstance(config, list) else config.get("observers", [])
    observers = raw if isinstance(raw, list) else []
    changed = raw is not observers
    found = False
    for item in observers:
        if isinstance(item, str):
            if item.strip() in legacy_spellings:
                found = True
        elif isinstance(item, dict):
            current_id = str(item.get("observer_id") or "").strip()
            if current_id == observer_id or (
                not current_id and str(item.get("factory") or "").strip() in legacy_spellings
            ):
                found = True
                if item.get("enabled") is False:
                    item["enabled"] = True
                    changed = True
    if not found:
        observers.append({"observer_id": observer_id, "factory": factory, "enabled": True})
        changed = True
    if changed:
        if isinstance(config, dict):
            config["observers"] = observers
        write_agent_config_json(path, config)
    return changed
