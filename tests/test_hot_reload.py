from __future__ import annotations

import types

import pytest

from orchestrator.hot_reload import (
    HotReloadError,
    detect_loaded_class_interface_changes,
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
        "orchestrator.her_v2.models",
        "orchestrator.her_v2.retry",
        "orchestrator.her_v2.runtime_configuration",
        "orchestrator.her_v2.interfaces",
        "orchestrator.her_v2.runtime",
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


def test_hot_reload_detects_new_method_on_loaded_config_class(tmp_path):
    source = tmp_path / "config.py"
    source.write_text(
        "class HERv2Config:\n"
        "    def profile_for(self, stage):\n"
        "        return stage\n"
        "\n"
        "    def profile_for_route(self, route, *, base_profile=None):\n"
        "        return route\n",
        encoding="utf-8",
    )
    module = types.ModuleType("orchestrator.her_v2.config")
    module.__file__ = str(source)

    class HERv2Config:
        def profile_for(self, stage):
            return stage

    HERv2Config.__module__ = module.__name__
    module.HERv2Config = HERv2Config

    assert detect_loaded_class_interface_changes(
        [module.__name__], modules={module.__name__: module}
    ) == [
        "orchestrator.her_v2.config.HERv2Config.profile_for_route (new method)"
    ]


def test_hot_reload_ignores_function_body_only_change(tmp_path):
    source = tmp_path / "runtime.py"
    source.write_text(
        "class Runtime:\n"
        "    def execute(self, request, *, effort=None):\n"
        "        return 'new implementation'\n",
        encoding="utf-8",
    )
    module = types.ModuleType("orchestrator.runtime_example")
    module.__file__ = str(source)

    class Runtime:
        def execute(self, request, *, effort=None):
            return "old implementation"

    Runtime.__module__ = module.__name__
    module.Runtime = Runtime

    assert (
        detect_loaded_class_interface_changes(
            [module.__name__], modules={module.__name__: module}
        )
        == []
    )


def test_hot_reload_ignores_unchanged_property_accessors(tmp_path):
    source = tmp_path / "context.py"
    source.write_text(
        "class Context:\n"
        "    @property\n"
        "    def memory_enabled(self):\n"
        "        return True\n"
        "\n"
        "    @memory_enabled.setter\n"
        "    def memory_enabled(self, enabled):\n"
        "        pass\n",
        encoding="utf-8",
    )
    module = types.ModuleType("orchestrator.context_example")
    module.__file__ = str(source)

    class Context:
        @property
        def memory_enabled(self):
            return False

        @memory_enabled.setter
        def memory_enabled(self, enabled):
            pass

    Context.__module__ = module.__name__
    module.Context = Context

    assert (
        detect_loaded_class_interface_changes(
            [module.__name__], modules={module.__name__: module}
        )
        == []
    )


def test_hot_reload_detects_property_setter_signature_change(tmp_path):
    source = tmp_path / "context.py"
    source.write_text(
        "class Context:\n"
        "    @property\n"
        "    def memory_enabled(self):\n"
        "        return True\n"
        "\n"
        "    @memory_enabled.setter\n"
        "    def memory_enabled(self, enabled, *, reason=None):\n"
        "        pass\n",
        encoding="utf-8",
    )
    module = types.ModuleType("orchestrator.context_example")
    module.__file__ = str(source)

    class Context:
        @property
        def memory_enabled(self):
            return False

        @memory_enabled.setter
        def memory_enabled(self, enabled):
            pass

    Context.__module__ = module.__name__
    module.Context = Context

    assert detect_loaded_class_interface_changes(
        [module.__name__], modules={module.__name__: module}
    ) == [
        "orchestrator.context_example.Context.memory_enabled.setter (signature changed)"
    ]


def test_hot_reload_detects_signature_and_dataclass_field_changes(tmp_path):
    source = tmp_path / "state.py"
    source.write_text(
        "class RuntimeState:\n"
        "    generation: int\n"
        "\n"
        "    def bind(self, request, *, context=None):\n"
        "        return request\n",
        encoding="utf-8",
    )
    module = types.ModuleType("orchestrator.runtime_state")
    module.__file__ = str(source)

    class RuntimeState:
        __dataclass_fields__ = {"request": object()}

        def bind(self, request):
            return request

    RuntimeState.__module__ = module.__name__
    module.RuntimeState = RuntimeState

    assert detect_loaded_class_interface_changes(
        [module.__name__], modules={module.__name__: module}
    ) == [
        "orchestrator.runtime_state.RuntimeState.generation (new field)",
        "orchestrator.runtime_state.RuntimeState.bind (signature changed)",
    ]
