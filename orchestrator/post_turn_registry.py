from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from typing import Any

from orchestrator.post_turn_observer import PostTurnObserver

# Core runtime code stays decoupled from concrete features and interacts only
# through the generic PostTurnObserver protocol. Concrete observer factories are
# declared outside core in workspace-level configuration.
logger = logging.getLogger("PostTurnRegistry")

OBSERVER_CONFIG = "post_turn_observers.json"


def build_post_turn_observers(
    *,
    workspace_dir: Path,
    bridge_memory_store: Any,
    backend_invoker: Any | None = None,
    backend_context_getter: Any | None = None,
) -> list[PostTurnObserver]:
    observers: list[PostTurnObserver] = []
    for spec in _load_observer_specs(workspace_dir):
        if not spec.get("enabled", True):
            continue
        factory_path = str(spec.get("factory", "")).strip()
        if not factory_path:
            logger.warning("Skipping post-turn observer with missing factory path")
            continue
        try:
            factory = _load_factory(factory_path)
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
            logger.warning("Failed to register post-turn observer factory %s: %s", factory_path, exc)
    return observers


def _load_observer_specs(workspace_dir: Path) -> list[dict[str, Any]]:
    path = workspace_dir / OBSERVER_CONFIG
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return []
    if isinstance(raw, dict):
        raw = raw.get("observers", [])
    if not isinstance(raw, list):
        logger.warning("%s must contain a list or an object with an observers list", path)
        return []
    specs: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            specs.append({"factory": item, "enabled": True})
        elif isinstance(item, dict):
            specs.append(dict(item))
    return specs


def _load_factory(factory_path: str):
    module_name, sep, attr_name = factory_path.partition(":")
    if not sep:
        module_name, sep, attr_name = factory_path.rpartition(".")
    if not module_name or not attr_name:
        raise ValueError(f"Invalid observer factory path: {factory_path!r}")
    module = importlib.import_module(module_name)
    factory = getattr(module, attr_name)
    if not callable(factory):
        raise TypeError(f"Observer factory is not callable: {factory_path}")
    return factory
