from __future__ import annotations

import types

import pytest

from orchestrator.hot_reload import (
    HotReloadError,
    discover_loaded_project_modules,
    module_reload_key,
    preflight_module_sources,
)


def test_hot_reload_preflight_rejects_syntax_error_before_mutation(tmp_path):
    source = tmp_path / "broken.py"
    source.write_text("def broken(:\n    pass\n", encoding="utf-8")
    module = types.ModuleType("orchestrator.broken")
    module.__file__ = str(source)

    with pytest.raises(HotReloadError, match="no agents were stopped"):
        preflight_module_sources(
            ["orchestrator.broken"],
            code_root=tmp_path,
            modules={"orchestrator.broken": module},
        )


def test_hot_reload_preflight_ignores_sources_outside_code_root(tmp_path):
    code_root = tmp_path / "project"
    code_root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("def broken(:\n", encoding="utf-8")
    module = types.ModuleType("orchestrator.external")
    module.__file__ = str(outside)

    assert (
        preflight_module_sources(
            ["orchestrator.external"],
            code_root=code_root,
            modules={"orchestrator.external": module},
        )
        == []
    )


def test_hot_reload_discovery_excludes_live_process_identity_modules(tmp_path):
    runtime_source = tmp_path / "orchestrator" / "runtime_status.py"
    runtime_source.parent.mkdir()
    runtime_source.write_text("STATUS = 'ok'\n", encoding="utf-8")
    runtime_module = types.ModuleType("orchestrator.runtime_status")
    runtime_module.__file__ = str(runtime_source)

    lock_source = tmp_path / "orchestrator" / "instance_lock.py"
    lock_source.write_text("LOCK = True\n", encoding="utf-8")
    lock_module = types.ModuleType("orchestrator.instance_lock")
    lock_module.__file__ = str(lock_source)

    discovered = discover_loaded_project_modules(
        {
            runtime_module.__name__: runtime_module,
            lock_module.__name__: lock_module,
        },
        code_root=tmp_path,
    )

    assert discovered == ["orchestrator.runtime_status"]


def test_hot_reload_discovery_rejects_prefixed_modules_outside_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "tools" / "external.py"
    outside.parent.mkdir()
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    module = types.ModuleType("tools.external")
    module.__file__ = str(outside)

    assert (
        discover_loaded_project_modules(
            {module.__name__: module},
            code_root=project,
        )
        == []
    )


def test_hot_reload_orders_adapter_protocol_before_consumers():
    names = [
        "adapters.her_v2",
        "adapters.openrouter_api",
        "adapters.base",
        "adapters.her_persona",
        "adapters.stream_io",
        "adapters.stream_events",
        "adapters.deepseek_api",
        "orchestrator.flexible_backend_manager",
        "orchestrator.her_v2.config",
        "orchestrator.her_v2.prompt_catalog",
        "orchestrator.her_v2.prompts",
        "orchestrator.her_v2.models",
        "orchestrator.her_v2.retry",
        "orchestrator.her_v2.runtime_configuration",
        "orchestrator.her_v2.interfaces",
        "orchestrator.her_v2.presentation",
        "orchestrator.her_v2.runtime_support",
        "orchestrator.her_v2.runtime_invocation",
        "orchestrator.her_v2.runtime",
        "adapters.her_v2_provider",
        "orchestrator.runtime_pipeline",
        "orchestrator.flexible_agent_runtime",
    ]

    ordered = sorted(names, key=module_reload_key)

    assert ordered.index("adapters.stream_events") < ordered.index("adapters.base")
    assert ordered.index("adapters.stream_io") < ordered.index("adapters.her_v2")
    assert ordered.index("adapters.base") < ordered.index("adapters.her_v2")
    assert ordered.index("adapters.her_persona") < ordered.index("adapters.her_v2")
    assert ordered.index("orchestrator.her_v2.models") < ordered.index(
        "orchestrator.her_v2.interfaces"
    )
    assert ordered.index("orchestrator.her_v2.interfaces") < ordered.index(
        "orchestrator.her_v2.runtime"
    )
    assert ordered.index("orchestrator.her_v2.prompt_catalog") < ordered.index(
        "orchestrator.her_v2.prompts"
    )
    assert ordered.index("orchestrator.her_v2.presentation") < ordered.index(
        "orchestrator.her_v2.runtime_support"
    )
    assert ordered.index("orchestrator.her_v2.runtime_support") < ordered.index(
        "orchestrator.her_v2.runtime_invocation"
    )
    assert ordered.index("orchestrator.her_v2.runtime_invocation") < ordered.index(
        "orchestrator.her_v2.runtime"
    )
    assert ordered.index("orchestrator.her_v2.retry") < ordered.index(
        "orchestrator.her_v2.runtime"
    )
    assert ordered.index("orchestrator.her_v2.config") < ordered.index(
        "orchestrator.her_v2.runtime_configuration"
    )
    assert ordered.index("orchestrator.her_v2.runtime_configuration") < ordered.index(
        "orchestrator.flexible_backend_manager"
    )
    assert ordered.index("orchestrator.her_v2.runtime") < ordered.index(
        "adapters.her_v2_provider"
    )
    assert ordered.index("adapters.her_v2_provider") < ordered.index(
        "adapters.her_v2"
    )
    assert ordered.index("adapters.openrouter_api") < ordered.index(
        "adapters.deepseek_api"
    )
    assert ordered.index("orchestrator.runtime_pipeline") < ordered.index(
        "orchestrator.flexible_agent_runtime"
    )


def test_hot_reload_discovery_skips_stale_module_without_source(tmp_path):
    stale = types.ModuleType("orchestrator.runtime_removed_feature")
    stale.__file__ = str(
        tmp_path
        / "orchestrator"
        / "__pycache__"
        / "runtime_removed_feature.cpython-312.pyc"
    )

    assert (
        discover_loaded_project_modules(
            {stale.__name__: stale},
            code_root=tmp_path,
        )
        == []
    )


def test_hot_reload_refreshes_stream_event_vocabulary_before_adapters():
    names = [
        "adapters.codex_cli",
        "adapters.stream_events",
        "adapters.base",
        "orchestrator.flexible_agent_runtime",
    ]

    assert sorted(names, key=module_reload_key) == [
        "adapters.stream_events",
        "adapters.base",
        "adapters.codex_cli",
        "orchestrator.flexible_agent_runtime",
    ]
