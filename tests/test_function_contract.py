from __future__ import annotations

import types

from orchestrator import function_contract
from orchestrator.function_contract import (
    discover_loaded_function_modules,
    function_module_order_key,
)


def _module(name, source):
    module = types.ModuleType(name)
    module.__file__ = str(source)
    return module


def test_function_discovery_selects_only_replaceable_project_sources(tmp_path):
    runtime_source = tmp_path / "orchestrator" / "runtime_status.py"
    runtime_source.parent.mkdir()
    runtime_source.write_text("STATUS = 'ok'\n", encoding="utf-8")
    core_source = tmp_path / "orchestrator" / "instance_lock.py"
    core_source.write_text("LOCK = True\n", encoding="utf-8")
    transport_source = tmp_path / "transports" / "whatsapp.py"
    transport_source.parent.mkdir()
    transport_source.write_text("TRANSPORT = True\n", encoding="utf-8")
    sidecar_source = tmp_path / "tools" / "windows_helper" / "server.py"
    sidecar_source.parent.mkdir(parents=True)
    sidecar_source.write_text("SIDECAR = True\n", encoding="utf-8")

    discovered = discover_loaded_function_modules(
        {
            "orchestrator.runtime_status": _module(
                "orchestrator.runtime_status", runtime_source
            ),
            "orchestrator.instance_lock": _module(
                "orchestrator.instance_lock", core_source
            ),
            "transports.whatsapp": _module(
                "transports.whatsapp", transport_source
            ),
            "tools.windows_helper.server": _module(
                "tools.windows_helper.server", sidecar_source
            ),
        },
        code_root=tmp_path,
    )

    assert discovered == [
        "orchestrator.runtime_status",
        "transports.whatsapp",
    ]


def test_function_discovery_rejects_external_and_deleted_sources(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "tools" / "external.py"
    outside.parent.mkdir()
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    external = _module("tools.external", outside)
    stale = _module(
        "orchestrator.removed",
        project / "orchestrator" / "removed.py",
    )

    assert (
        discover_loaded_function_modules(
            {external.__name__: external, stale.__name__: stale},
            code_root=project,
        )
        == []
    )


def test_function_order_fallback_is_deterministic_for_dependency_cycles():
    names = [
        "orchestrator.flexible_agent_runtime",
        "tools.registry",
        "adapters.deepseek_api",
    ]

    first = sorted(names, key=function_module_order_key)
    second = sorted(reversed(names), key=function_module_order_key)

    assert first == second


def test_in_process_hot_reload_vocabulary_is_not_exported():
    assert not hasattr(function_contract, "HotReloadError")
    assert not hasattr(function_contract, "HOT_RELOAD_PREFIXES")
    assert not hasattr(function_contract, "module_reload_key")
    assert not hasattr(function_contract, "discover_loaded_project_modules")
