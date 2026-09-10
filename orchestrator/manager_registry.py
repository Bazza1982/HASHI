from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Literal

ManagerConstructor = Literal["empty", "paths", "kernel", "kernel_console", "skill"]


@dataclass(frozen=True)
class ManagerSpec:
    """One authoritative construction rule for a shared Function manager."""

    attribute: str
    module: str
    class_name: str
    constructor: ManagerConstructor


FUNCTION_MANAGER_SPECS: tuple[ManagerSpec, ...] = (
    ManagerSpec(
        "endpoint_registry",
        "orchestrator.service_endpoints",
        "ServiceEndpointRegistry",
        "kernel",
    ),
    ManagerSpec(
        "capability_broker",
        "orchestrator.capability_broker",
        "CapabilityBroker",
        "kernel",
    ),
    ManagerSpec("skill_manager", "orchestrator.skill_manager", "SkillManager", "skill"),
    ManagerSpec("config_admin", "orchestrator.config_admin", "ConfigAdmin", "paths"),
    ManagerSpec("backend_preflight", "orchestrator.backend_preflight", "BackendPreflight", "empty"),
    ManagerSpec("agent_lifecycle", "orchestrator.agent_lifecycle", "AgentLifecycleManager", "kernel"),
    ManagerSpec("agent_move_manager", "orchestrator.agent_move.manager", "AgentMoveManager", "kernel"),
    ManagerSpec("service_manager", "orchestrator.service_manager", "ServiceManager", "kernel"),
    ManagerSpec("reboot_manager", "orchestrator.reboot_manager", "RebootManager", "kernel_console"),
    ManagerSpec("shutdown_manager", "orchestrator.shutdown_manager", "ShutdownManager", "kernel"),
    ManagerSpec("startup_manager", "orchestrator.startup_manager", "StartupManager", "kernel_console"),
    ManagerSpec("whatsapp_manager", "orchestrator.whatsapp_manager", "WhatsAppManager", "kernel"),
)

def _construct_manager(spec: ManagerSpec, manager_class, kernel, console_handler):
    if spec.constructor == "empty":
        return manager_class()
    if spec.constructor == "skill":
        return manager_class(kernel.paths.code_root, kernel.paths.tasks_path)
    if spec.constructor == "paths":
        return manager_class(kernel.paths)
    if spec.constructor == "kernel_console":
        return manager_class(kernel, console_handler)
    if spec.constructor == "kernel":
        return manager_class(kernel)
    raise ValueError(f"Unknown manager constructor rule: {spec.constructor}")


def build_function_manager_bundle(
    kernel,
    console_handler,
    *,
    module_loader: Callable[[str], ModuleType] = importlib.import_module,
) -> dict[str, object]:
    """Construct the shared Function manager set at process startup."""
    bundle: dict[str, object] = {}
    for spec in FUNCTION_MANAGER_SPECS:
        module = module_loader(spec.module)
        manager_class = getattr(module, spec.class_name)
        bundle[spec.attribute] = _construct_manager(
            spec,
            manager_class,
            kernel,
            console_handler,
        )
    return bundle


def install_function_manager_bundle(
    kernel,
    bundle: dict[str, object],
    *,
    module_loader: Callable[[str], ModuleType] = importlib.import_module,
) -> None:
    """Install a complete Function manager bundle inside the shared process."""

    expected = {spec.attribute for spec in FUNCTION_MANAGER_SPECS}
    if set(bundle) != expected:
        missing = sorted(expected - set(bundle))
        extra = sorted(set(bundle) - expected)
        raise ValueError(f"Invalid manager bundle; missing={missing}, extra={extra}")

    for spec in FUNCTION_MANAGER_SPECS:
        setattr(kernel, spec.attribute, bundle[spec.attribute])
