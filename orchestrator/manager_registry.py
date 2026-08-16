from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Literal

ManagerConstructor = Literal["empty", "paths", "kernel", "kernel_console", "skill"]


@dataclass(frozen=True)
class ManagerSpec:
    """One authoritative construction rule for a kernel-owned manager."""

    attribute: str
    module: str
    class_name: str
    constructor: ManagerConstructor


HOT_MANAGER_SPECS: tuple[ManagerSpec, ...] = (
    ManagerSpec("skill_manager", "orchestrator.skill_manager", "SkillManager", "skill"),
    ManagerSpec("config_admin", "orchestrator.config_admin", "ConfigAdmin", "paths"),
    ManagerSpec("backend_preflight", "orchestrator.backend_preflight", "BackendPreflight", "empty"),
    ManagerSpec("agent_lifecycle", "orchestrator.agent_lifecycle", "AgentLifecycleManager", "kernel"),
    ManagerSpec("service_manager", "orchestrator.service_manager", "ServiceManager", "kernel"),
    ManagerSpec("reboot_manager", "orchestrator.reboot_manager", "RebootManager", "kernel_console"),
    ManagerSpec("shutdown_manager", "orchestrator.shutdown_manager", "ShutdownManager", "kernel"),
    ManagerSpec("startup_manager", "orchestrator.startup_manager", "StartupManager", "kernel_console"),
    ManagerSpec("whatsapp_manager", "orchestrator.whatsapp_manager", "WhatsAppManager", "kernel"),
)

# These coordinators must survive the restart they supervise.  They are
# installed once for a new kernel and are deliberately excluded from the hot
# bundle rebuilt on every /reboot.  Keeping the manifest here also lets an
# already-running pre-feature kernel acquire a newly-added stable manager on
# its first hot reload.
STABLE_MANAGER_SPECS: tuple[ManagerSpec, ...] = (
    ManagerSpec(
        "her_rebuild_manager",
        "orchestrator.her_rebuild_manager",
        "HERRebuildManager",
        "kernel",
    ),
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


def build_hot_manager_bundle(
    kernel,
    console_handler,
    *,
    module_loader: Callable[[str], ModuleType] = importlib.import_module,
) -> dict[str, object]:
    """Build every manager before mutating the live kernel."""
    bundle: dict[str, object] = {}
    for spec in HOT_MANAGER_SPECS:
        module = module_loader(spec.module)
        manager_class = getattr(module, spec.class_name)
        bundle[spec.attribute] = _construct_manager(
            spec,
            manager_class,
            kernel,
            console_handler,
        )
    return bundle


def install_hot_manager_bundle(
    kernel,
    bundle: dict[str, object],
    *,
    module_loader: Callable[[str], ModuleType] = importlib.import_module,
) -> None:
    expected = {spec.attribute for spec in HOT_MANAGER_SPECS}
    if set(bundle) != expected:
        missing = sorted(expected - set(bundle))
        extra = sorted(set(bundle) - expected)
        raise ValueError(f"Invalid manager bundle; missing={missing}, extra={extra}")

    # Construct every missing stable manager before mutating the hot bundle.
    # Existing instances are upgraded in place: an in-flight /rebuild owns
    # state and tasks that must outlive the targeted /reboot it requests.
    stable_additions: dict[str, object] = {}
    for spec in STABLE_MANAGER_SPECS:
        module = module_loader(spec.module)
        manager_class = getattr(module, spec.class_name)
        existing = getattr(kernel, spec.attribute, None)
        if existing is not None:
            upgrader = getattr(manager_class, "upgrade_existing", None)
            if not callable(upgrader):
                raise TypeError(
                    f"Stable manager {spec.attribute!r} does not support hot upgrade"
                )
            upgraded = upgrader(existing)
            if upgraded is not existing:
                raise ValueError(
                    f"Stable manager {spec.attribute!r} hot upgrade replaced its instance"
                )
            continue
        stable_additions[spec.attribute] = _construct_manager(
            spec,
            manager_class,
            kernel,
            console_handler=None,
        )

    for spec in HOT_MANAGER_SPECS:
        setattr(kernel, spec.attribute, bundle[spec.attribute])
    for spec in STABLE_MANAGER_SPECS:
        if spec.attribute in stable_additions:
            setattr(kernel, spec.attribute, stable_additions[spec.attribute])
