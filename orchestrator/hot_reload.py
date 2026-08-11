from __future__ import annotations

import py_compile
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType


HOT_RELOAD_PREFIXES = ("adapters.", "tools.", "orchestrator.")

# These modules define the identity and lock of the already-running process.
# Reloading their module objects cannot replace that live bootstrap state and
# would falsely suggest that a cold-only change had taken effect.
COLD_RESTART_MODULES = frozenset(
    {
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
    "adapters.stream_events": 0,
    "adapters.stream_io": 0,
    "orchestrator.flexible_backend_registry": 0,
    "orchestrator.command_specs": 0,
    "orchestrator.runtime_defaults": 0,
    "orchestrator.workspace_state": 0,
    "adapters.base": 1,
    "adapters.xai_oauth_credentials": 1,
    "orchestrator.model_catalog": 1,
    "orchestrator.manager_registry": 1,
    "orchestrator.ticket_manager": 1,
    "adapters.openrouter_api": 2,
    "adapters.xai_imagine": 2,
    # HER owns the implementation; the legacy claw_cli facade must reload
    # afterwards so all of its compatibility exports point at current objects.
    "adapters.her": 2,
    "adapters.claw_cli": 3,
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
        if name in COLD_RESTART_MODULES:
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
        try:
            path.resolve().relative_to(root)
        except (OSError, ValueError):
            return False
        return True

    return sorted(
        (
            name
            for name in list(loaded)
            if is_reloadable_project_module(name)
        ),
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
