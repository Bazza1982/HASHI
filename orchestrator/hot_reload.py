from __future__ import annotations

import py_compile
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

HOT_RELOAD_PREFIXES = ("adapters.", "tools.", "orchestrator.")

# These modules define identity objects already owned by the running process.
# They are not function-layer modules: changing one is incomplete until it has
# an explicit warm-handoff design.  /reboot must never claim that merely
# reloading the module replaced an already-held lock or path identity.
PROCESS_IDENTITY_MODULES = frozenset(
    {
        # Retired execution modules may remain imported by historical tests or
        # a pre-upgrade process, but hot reload must never reactivate them.
        "adapters.her",
        "adapters.claw_cli",
        "orchestrator.instance_lock",
        "orchestrator.pathing",
    }
)

# Only dependency roots that are imported by many consumers belong here.
# The order is centralized so /reboot has one reload contract.  In particular,
# adapter protocol modules must be refreshed before adapters that import their
# constants/classes at module import time.  Otherwise a hot reload can combine
# new consumer source with the previous in-memory protocol module.
FOUNDATION_PHASES = {
    # The HER gateway context imports ToolRegistry at module scope.  Reload
    # schemas, then the registry, then the context so a hot restart cannot
    # retain the pre-change ToolRegistry class after its constructor evolves.
    "tools.schemas": 0,
    "tools.registry": 1,
    "tools.gateway.context": 2,
    "tools.gateway.mcp_stdio": 3,
    "adapters.stream_events": 0,
    "adapters.stream_io": 0,
    "orchestrator.flexible_backend_registry": 0,
    "orchestrator.command_specs": 0,
    # QueuedRequest is imported at module scope by both agent runtimes and
    # request-pipeline consumers. Reload it first so a hot reboot cannot bind
    # a new enqueue method to the previous dataclass constructor.
    "orchestrator.runtime_common": 0,
    "orchestrator.runtime_defaults": 0,
    "orchestrator.workspace_state": 0,
    "adapters.base": 1,
    "adapters.her_persona": 1,
    "adapters.xai_oauth_credentials": 1,
    "orchestrator.model_catalog": 1,
    "orchestrator.manager_registry": 1,
    "orchestrator.ticket_manager": 1,
    "adapters.openrouter_api": 2,
    "adapters.xai_imagine": 2,
    # HER v2 dependency order. Reload value types first and the facade only
    # after the provider-neutral runtime graph is coherent.
    "orchestrator.her_v2.models": 0,
    "orchestrator.her_v2.audit": 0,
    "orchestrator.her_v2.progress": 0,
    "orchestrator.her_v2.config": 1,
    "orchestrator.her_v2.lifecycle": 1,
    "orchestrator.her_v2.policy": 1,
    "orchestrator.her_v2.prompts": 1,
    "orchestrator.her_v2.interfaces": 2,
    "orchestrator.her_v2.ledger": 3,
    "orchestrator.her_v2.learning": 3,
    "orchestrator.her_v2.structured": 3,
    "orchestrator.her_v2.commentary": 4,
    "orchestrator.her_v2.runtime": 4,
    "orchestrator.her_v2": 5,
    "adapters.her_v2": 5,
}


class HotReloadError(RuntimeError):
    pass


def module_reload_key(name: str) -> tuple[int, str]:
    if name in FOUNDATION_PHASES:
        return (FOUNDATION_PHASES[name], name)
    if name.startswith(("adapters.", "tools.")):
        return (3, name)
    if "_runtime" in name:
        return (5, name)
    return (4, name)


def discover_loaded_project_modules(
    modules: Mapping[str, ModuleType] | None = None,
    *,
    code_root: Path | None = None,
) -> list[str]:
    loaded = modules if modules is not None else sys.modules
    root = Path(code_root).resolve() if code_root is not None else None

    def is_reloadable_project_module(name: str) -> bool:
        if name in PROCESS_IDENTITY_MODULES:
            return False
        if not any(name.startswith(prefix) for prefix in HOT_RELOAD_PREFIXES):
            return False
        if root is None:
            return True
        module = loaded.get(name)
        raw_file = getattr(module, "__file__", None) if module is not None else None
        if not raw_file:
            return False
        path = Path(raw_file)
        if path.suffix in {".pyc", ".pyo"}:
            path = Path(str(path)[:-1])
        # A long-running process may still retain a module from a branch that
        # has since been switched away. importlib.reload() cannot reload that
        # stale object once its source file is gone, so exclude it up front.
        if not path.is_file():
            return False
        try:
            path.resolve().relative_to(root)
        except (OSError, ValueError):
            return False
        return True

    return sorted(
        (name for name in list(loaded) if is_reloadable_project_module(name)),
        key=module_reload_key,
    )


def preflight_module_sources(
    module_names: list[str],
    *,
    code_root: Path,
    modules: Mapping[str, ModuleType] | None = None,
) -> list[Path]:
    """Compile every loaded project source before any live module is mutated."""
    loaded = modules if modules is not None else sys.modules
    root = Path(code_root).resolve()
    checked: list[Path] = []
    failures: list[str] = []
    seen: set[Path] = set()
    for name in module_names:
        module = loaded.get(name)
        raw_file = getattr(module, "__file__", None) if module is not None else None
        if not raw_file:
            continue
        path = Path(raw_file)
        if path.suffix in {".pyc", ".pyo"}:
            source_path = Path(str(path)[:-1])
            if source_path.exists():
                path = source_path
        if path.suffix != ".py" or not path.exists():
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            py_compile.compile(str(resolved), doraise=True)
            checked.append(resolved)
        except py_compile.PyCompileError as exc:
            failures.append(f"{name}: {exc.msg}")
    if failures:
        raise HotReloadError(
            "Hot reload preflight failed; no agents were stopped:\n"
            + "\n".join(failures)
        )
    return checked
