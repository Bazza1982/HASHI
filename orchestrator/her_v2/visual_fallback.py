"""Shared HERV3 visual fallback availability rules.

Frontend admission and Provider invocation must consult the same Agent-scoped
model list.  This module owns the pure availability projection; the Provider
bridge still performs the authoritative per-request routing and validation.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orchestrator.compute_services import ComputeServiceError, ComputeServicesConfig
from orchestrator.flexible_backend_registry import canonical_model_provider
from orchestrator.her_v2.config import ProviderProfile


def configured_same_provider_models(
    backend_manager: Any,
    profile: ProviderProfile,
) -> tuple[str, ...]:
    """Return this Agent's ordered available models for one Model Provider."""

    options: list[dict[str, Any]] = []
    for getter_name in (
        "get_her_v3_provider_options",
        "get_her_v2_provider_options",
    ):
        getter = getattr(backend_manager, getter_name, None)
        if not callable(getter):
            continue
        try:
            options = [
                dict(item)
                for item in getter()
                if isinstance(item, Mapping)
            ]
        except Exception:
            options = []
        if options:
            break

    selected_provider = canonical_model_provider(profile.engine)
    for option in options:
        option_provider = canonical_model_provider(str(option.get("engine") or ""))
        if option_provider != selected_provider or option.get("available") is False:
            continue
        models: list[str] = []
        for raw in option.get("models") or ():
            model = str(raw or "").strip()
            if model and model not in models:
                models.append(model)
        return tuple(models)
    return ()


def lan_vision_configured(config_path: str | Path | None) -> bool:
    """Return whether an enabled LAN compute node is durably configured."""

    if config_path is None:
        return False
    try:
        _document, nodes = ComputeServicesConfig(config_path).read()
    except (ComputeServiceError, OSError, TypeError, ValueError):
        return False
    return any(node.enabled and node.location == "lan" for node in nodes)


__all__ = [
    "configured_same_provider_models",
    "lan_vision_configured",
]
