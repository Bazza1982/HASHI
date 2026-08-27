from __future__ import annotations

import asyncio
import runpy
import sys
import types
from contextlib import suppress
from types import SimpleNamespace

import pytest

from adapters import registry as backend_registry
from orchestrator import reboot_manager as reboot_manager_module
from orchestrator.hot_reload import HotReloadError, module_reload_key
from orchestrator.reboot_manager import RebootManager, _resolve_restart_targets
from orchestrator.service_manager import ServiceManager


def test_reload_project_modules_includes_tools(monkeypatch):
    manager = RebootManager(kernel=object(), console_handler=None)
    module_names = [
        "adapters.sample_adapter",
        "tools.hchat_send",
        "orchestrator.hchat_delivery",
        "orchestrator.runtime_pipeline",
        "orchestrator.runtime_status",
        "orchestrator.telegram_delivery_failover",
        "orchestrator.telegram_stream_policy",
        "external.module",
    ]
    modules = {name: types.ModuleType(name) for name in module_names}
    reloaded = []

    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    def fake_reload(module):
        reloaded.append(module.__name__)
        return module

    monkeypatch.setattr("orchestrator.reboot_manager.importlib.reload", fake_reload)

    manager.reload_project_modules()

    assert "adapters.sample_adapter" in reloaded
    assert "tools.hchat_send" in reloaded
    assert "orchestrator.hchat_delivery" in reloaded
    assert "orchestrator.runtime_pipeline" in reloaded
    assert "orchestrator.runtime_status" in reloaded
    assert "orchestrator.telegram_delivery_failover" in reloaded
    assert "orchestrator.telegram_stream_policy" in reloaded
    assert "external.module" not in reloaded


def test_reload_project_modules_loads_model_foundations_before_consumers(monkeypatch):
    manager = RebootManager(kernel=object(), console_handler=None)
    module_names = [
        "orchestrator.flexible_agent_runtime",
        "adapters.codex_cli",
        "orchestrator.flexible_backend_registry",
        "orchestrator.model_catalog",
        "orchestrator.flexible_backend_manager",
    ]
    reloaded = []

    for name in module_names:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))

    def fake_reload(module):
        reloaded.append(module.__name__)
        return module

    monkeypatch.setattr("orchestrator.reboot_manager.importlib.reload", fake_reload)

    manager.reload_project_modules()

    catalog_idx = reloaded.index("orchestrator.model_catalog")
    registry_idx = reloaded.index("orchestrator.flexible_backend_registry")
    adapter_idx = reloaded.index("adapters.codex_cli")
    manager_idx = reloaded.index("orchestrator.flexible_backend_manager")
    runtime_idx = reloaded.index("orchestrator.flexible_agent_runtime")

    assert registry_idx < catalog_idx < adapter_idx < manager_idx < runtime_idx


def test_reload_project_modules_loads_runtime_defaults_before_consumers(monkeypatch):
    manager = RebootManager(kernel=object(), console_handler=None)
    module_names = [
        "orchestrator.flexible_agent_runtime",
        "orchestrator.runtime_defaults",
        "orchestrator.remote_lifecycle",
    ]
    reloaded = []

    for name in module_names:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))

    def fake_reload(module):
        reloaded.append(module.__name__)
        return module

    monkeypatch.setattr("orchestrator.reboot_manager.importlib.reload", fake_reload)

    manager.reload_project_modules()

    defaults_idx = reloaded.index("orchestrator.runtime_defaults")
    lifecycle_idx = reloaded.index("orchestrator.remote_lifecycle")
    runtime_idx = reloaded.index("orchestrator.flexible_agent_runtime")
    assert defaults_idx < lifecycle_idx < runtime_idx


def test_reload_project_modules_loads_instance_provider_before_consumers(monkeypatch):
    manager = RebootManager(kernel=object(), console_handler=None)
    module_names = [
        "orchestrator.private_wol",
        "orchestrator.ticket_manager",
    ]
    reloaded = []

    for name in module_names:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))

    def fake_reload(module):
        reloaded.append(module.__name__)
        return module

    monkeypatch.setattr("orchestrator.reboot_manager.importlib.reload", fake_reload)

    manager.reload_project_modules()

    provider_idx = reloaded.index("orchestrator.ticket_manager")
    assert provider_idx < reloaded.index("orchestrator.private_wol")


def test_reload_project_modules_loads_stream_policy_before_flexible_runtime(monkeypatch):
    manager = RebootManager(kernel=object(), console_handler=None)
    module_names = [
        "orchestrator.flexible_agent_runtime",
        "orchestrator.runtime_pipeline",
        "orchestrator.runtime_status",
        "orchestrator.telegram_stream_policy",
    ]
    reloaded = []

    for name in module_names:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))

    def fake_reload(module):
        reloaded.append(module.__name__)
        return module

    monkeypatch.setattr("orchestrator.reboot_manager.importlib.reload", fake_reload)

    manager.reload_project_modules()

    policy_idx = reloaded.index("orchestrator.telegram_stream_policy")
    pipeline_idx = reloaded.index("orchestrator.runtime_pipeline")
    status_idx = reloaded.index("orchestrator.runtime_status")
    runtime_idx = reloaded.index("orchestrator.flexible_agent_runtime")
    assert policy_idx < runtime_idx
    assert pipeline_idx < runtime_idx
    assert status_idx < runtime_idx


def test_reload_project_modules_loads_runtime_common_before_consumers(monkeypatch):
    manager = RebootManager(kernel=object(), console_handler=None)
    module_names = [
        "orchestrator.flexible_agent_runtime",
        "orchestrator.runtime_common",
        "orchestrator.runtime_pipeline",
    ]
    reloaded = []

    for name in module_names:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))

    def fake_reload(module):
        reloaded.append(module.__name__)
        return module

    monkeypatch.setattr("orchestrator.reboot_manager.importlib.reload", fake_reload)

    manager.reload_project_modules()

    common_idx = reloaded.index("orchestrator.runtime_common")
    assert common_idx < reloaded.index("orchestrator.flexible_agent_runtime")
    assert common_idx < reloaded.index("orchestrator.runtime_pipeline")


def test_reload_project_modules_loads_tool_registry_before_gateway_context(monkeypatch):
    manager = RebootManager(kernel=object(), console_handler=None)
    module_names = [
        "tools.gateway.mcp_stdio",
        "tools.gateway.context",
        "tools.registry",
        "tools.schemas",
    ]
    reloaded = []

    for name in module_names:
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))

    def fake_reload(module):
        reloaded.append(module.__name__)
        return module

    monkeypatch.setattr("orchestrator.reboot_manager.importlib.reload", fake_reload)

    manager.reload_project_modules(sorted(module_names, key=module_reload_key))

    assert reloaded == [
        "tools.schemas",
        "tools.registry",
        "tools.gateway.context",
        "tools.gateway.mcp_stdio",
    ]


def test_validate_agent_runtime_contract_accepts_current_modules():
    manager = RebootManager(kernel=object(), console_handler=None)

    manager.validate_agent_runtime_contract()


def test_validate_agent_runtime_contract_rejects_legacy_notification_signature(
    monkeypatch,
):
    manager = RebootManager(kernel=object(), console_handler=None)
    monkeypatch.setattr(
        "orchestrator.telegram_notifications.disable_notification",
        lambda runtime: not bool(runtime),
    )

    with pytest.raises(HotReloadError, match="notification mode"):
        manager.validate_agent_runtime_contract()


def test_validate_agent_runtime_contract_requires_notify_command(monkeypatch):
    manager = RebootManager(kernel=object(), console_handler=None)
    monkeypatch.setattr(
        "orchestrator.command_registry.runtime_command_map",
        lambda: {},
    )

    with pytest.raises(HotReloadError, match="/notify command"):
        manager.validate_agent_runtime_contract()


def test_reload_hands_current_contract_to_live_legacy_manager():
    class LegacyRebootManager:
        def validate_agent_runtime_contract(self):
            supported_adapter = backend_registry.get_backend_class("her-v2")
            assert all(
                backend_registry.get_backend_class(engine) is supported_adapter
                for engine in ("her-v2", "her", "claw-cli")
            )

    legacy_manager = LegacyRebootManager()
    with pytest.raises(ValueError, match="Unknown engine: claw-cli"):
        legacy_manager.validate_agent_runtime_contract()

    reloaded_namespace = runpy.run_path(
        reboot_manager_module.__file__,
        init_globals={"RebootManager": LegacyRebootManager},
    )
    reloaded_manager_class = reloaded_namespace["RebootManager"]

    legacy_manager.validate_agent_runtime_contract()
    assert (
        LegacyRebootManager.validate_agent_runtime_contract
        is reloaded_manager_class.validate_agent_runtime_contract
    )
    with pytest.raises(ValueError, match="Unknown engine: claw-cli"):
        backend_registry.get_backend_class("claw-cli")


def test_handoff_repairs_kernel_manager_stranded_before_module_generation():
    class OldestRebootManager:
        def validate_agent_runtime_contract(self):
            backend_registry.get_backend_class("claw-cli")

    class IntermediateRebootManager(OldestRebootManager):
        pass

    for manager_class in (OldestRebootManager, IntermediateRebootManager):
        manager_class.__name__ = "RebootManager"
        manager_class.__module__ = "orchestrator.reboot_manager_stranded_test"

    oldest_manager = OldestRebootManager()
    intermediate_manager = IntermediateRebootManager()
    reloaded_namespace = runpy.run_path(
        reboot_manager_module.__file__,
        init_globals={"RebootManager": IntermediateRebootManager},
    )
    reloaded_manager_class = reloaded_namespace["RebootManager"]

    assert reloaded_namespace["_HANDED_OFF_REBOOT_MANAGER_GENERATIONS"] == 2
    oldest_manager.validate_agent_runtime_contract()
    intermediate_manager.validate_agent_runtime_contract()
    assert (
        OldestRebootManager.validate_agent_runtime_contract
        is reloaded_manager_class.validate_agent_runtime_contract
    )
    assert (
        IntermediateRebootManager.validate_agent_runtime_contract
        is reloaded_manager_class.validate_agent_runtime_contract
    )
    with pytest.raises(ValueError, match="Unknown engine: claw-cli"):
        backend_registry.get_backend_class("claw-cli")


def test_validate_agent_runtime_contract_rejects_stale_tool_registry(monkeypatch):
    manager = RebootManager(kernel=object(), console_handler=None)

    monkeypatch.setattr("tools.gateway.context.ToolRegistry", object())

    with pytest.raises(HotReloadError, match="stale ToolRegistry class"):
        manager.validate_agent_runtime_contract()


def test_validate_agent_runtime_contract_rejects_missing_scoped_audit_context(
    monkeypatch,
):
    manager = RebootManager(kernel=object(), console_handler=None)

    monkeypatch.setattr(
        "tools.registry.ToolRegistry.execute_with_audit_context",
        None,
    )

    with pytest.raises(HotReloadError, match="scoped audit context is unavailable"):
        manager.validate_agent_runtime_contract()


def test_validate_agent_runtime_contract_rejects_missing_her_v2_resolver(monkeypatch):
    manager = RebootManager(kernel=object(), console_handler=None)

    monkeypatch.setattr(backend_registry, "get_backend_class", None)

    with pytest.raises(HotReloadError, match="registry contract unavailable"):
        manager.validate_agent_runtime_contract()


def test_validate_agent_runtime_contract_rejects_retired_her_route(monkeypatch):
    manager = RebootManager(kernel=object(), console_handler=None)
    current_resolver = backend_registry.get_backend_class

    monkeypatch.setattr(
        backend_registry,
        "get_backend_class",
        lambda engine: object() if engine == "her" else current_resolver(engine),
    )

    with pytest.raises(HotReloadError, match="retired adapter"):
        manager.validate_agent_runtime_contract()


def test_validate_agent_runtime_contract_rejects_stale_queued_request(monkeypatch):
    manager = RebootManager(kernel=object(), console_handler=None)

    monkeypatch.setattr("orchestrator.flexible_agent_runtime.QueuedRequest", object())

    with pytest.raises(HotReloadError, match="stale QueuedRequest class"):
        manager.validate_agent_runtime_contract()


def test_reload_project_modules_fails_fast_instead_of_continuing(monkeypatch):
    manager = RebootManager(kernel=object(), console_handler=None)
    first = types.ModuleType("orchestrator.first")
    broken = types.ModuleType("orchestrator.broken")
    after = types.ModuleType("orchestrator.after")
    for module in (first, broken, after):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    calls = []

    def fake_reload(module):
        calls.append(module.__name__)
        if module is broken:
            raise RuntimeError("boom")
        return module

    monkeypatch.setattr("orchestrator.reboot_manager.importlib.reload", fake_reload)

    with pytest.raises(HotReloadError, match="orchestrator.broken"):
        manager.reload_project_modules(
            ["orchestrator.first", "orchestrator.broken", "orchestrator.after"]
        )

    assert calls == ["orchestrator.first", "orchestrator.broken"]


@pytest.mark.parametrize(
    ("restart", "expected"),
    [
        ({"mode": "min", "agent_name": "zelda"}, ("zelda",)),
        ({"mode": "number", "agent_number": 3}, ("offline",)),
        ({"mode": "same"}, ("zelda", "sunny")),
        ({"mode": "max"}, ("zelda", "sunny")),
    ],
)
def test_restart_scope_requires_an_explicit_broad_mode(restart, expected):
    kernel = SimpleNamespace(
        runtimes=[SimpleNamespace(name="zelda"), SimpleNamespace(name="sunny")],
        configured_agent_names=lambda: ["zelda", "sunny", "offline"],
    )

    targets = _resolve_restart_targets(kernel, restart)

    assert targets == expected
    if restart["mode"] in {"min", "number"}:
        assert len(targets) == 1


@pytest.mark.parametrize(
    "restart",
    [
        {"mode": "min"},
        {"mode": "number", "agent_number": 0},
        {"mode": "number", "agent_number": 4},
        {"mode": "number", "agent_number": "2"},
        {"mode": "unexpected", "agent_name": "zelda"},
    ],
)
def test_invalid_restart_scope_is_rejected_instead_of_falling_back_to_all(restart):
    kernel = SimpleNamespace(
        runtimes=[SimpleNamespace(name="zelda"), SimpleNamespace(name="sunny")],
        configured_agent_names=lambda: ["zelda", "sunny", "offline"],
    )

    with pytest.raises(ValueError):
        _resolve_restart_targets(kernel, restart)


def test_restart_scope_guard_stays_outside_manager_class_for_legacy_min_adoption():
    assert "_resolve_restart_targets" not in RebootManager.__dict__
    assert callable(reboot_manager_module._resolve_restart_targets)


@pytest.mark.asyncio
async def test_hot_restart_fails_when_target_does_not_restart_even_if_others_run(
    monkeypatch,
):
    class ConsoleHandler:
        def addFilter(self, _filter):
            pass

        def removeFilter(self, _filter):
            pass

    class HotServices:
        def __init__(self):
            self.refreshed = False

        async def refresh_hot_services(self):
            self.refreshed = True

    class Kernel:
        def __init__(self):
            self.runtimes = [
                SimpleNamespace(name=f"other-{index}")
                for index in range(19)
            ]
            self.whatsapp = None
            self.global_cfg = SimpleNamespace(workbench_port=18800)
            self.api_gateway = None
            self.service_manager = HotServices()

        async def stop_agent(self, _name, reason):
            assert reason == "hot-restart:min"
            return True, "stopped"

        async def start_agent(self, name):
            assert name == "lily"
            return False, "detect_instance() takes 1 positional argument but 2 were given"

        def _load_config_bundle(self):
            return None, [SimpleNamespace(name="lily")], None

    kernel = Kernel()
    manager = RebootManager(kernel=kernel, console_handler=ConsoleHandler())
    monkeypatch.setattr(manager, "preflight_project_modules", lambda: [])
    monkeypatch.setattr(manager, "reload_project_modules", lambda _names: None)
    monkeypatch.setattr(manager, "rebuild_hot_managers", lambda: None)
    monkeypatch.setattr(
        "orchestrator.banner.show_startup_banner",
        lambda **_kwargs: None,
    )

    result = await manager.hot_restart(
        {"mode": "min", "agent_name": "lily", "agent_number": None}
    )

    assert result is False
    assert len(kernel.runtimes) == 19
    assert kernel.service_manager.refreshed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("restart", "expected_target"),
    [
        (
            {"mode": "min", "agent_name": "zelda", "agent_number": None},
            "zelda",
        ),
        (
            {"mode": "number", "agent_name": "zelda", "agent_number": 2},
            "sunny",
        ),
    ],
)
async def test_targeted_hot_restart_loads_public_api_change_without_widening_scope(
    monkeypatch,
    tmp_path,
    restart,
    expected_target,
):
    class ConsoleHandler:
        def addFilter(self, _filter):
            pass

        def removeFilter(self, _filter):
            pass

    class HotServices:
        def __init__(self):
            self.refreshed = False

        async def refresh_hot_services(self):
            self.refreshed = True

    class Runtime:
        def __init__(self, name):
            self.name = name

    class Kernel:
        def __init__(self):
            self.runtimes = [
                Runtime("zelda"),
                Runtime("sunny"),
            ]
            self.whatsapp = None
            self.global_cfg = SimpleNamespace(workbench_port=18800)
            self.api_gateway = None
            self.service_manager = HotServices()
            self.stop_calls = []
            self.start_calls = []

        async def stop_agent(self, name, reason):
            self.stop_calls.append((name, reason))
            self.runtimes[:] = [
                runtime for runtime in self.runtimes if runtime.name != name
            ]
            return True, "stopped"

        async def start_agent(self, name):
            assert code_state["loaded"] is True
            self.start_calls.append(name)
            self.runtimes.append(SimpleNamespace(name=name))
            return True, "started"

        def _load_config_bundle(self):
            return (
                None,
                [SimpleNamespace(name="zelda"), SimpleNamespace(name="sunny")],
                None,
            )

        def configured_agent_names(self):
            return ["zelda", "sunny", "offline"]

    kernel = Kernel()
    untouched_runtime = next(
        runtime for runtime in kernel.runtimes if runtime.name != expected_target
    )
    manager = RebootManager(kernel=kernel, console_handler=ConsoleHandler())

    source = tmp_path / "reboot_scope_fixture.py"
    source.write_text(
        "class RuntimeGeneration:\n"
        "    def execute(self, request):\n"
        "        return request\n"
        "\n"
        "    def added_public_method(self, value, *, enabled=True):\n"
        "        return value if enabled else None\n",
        encoding="utf-8",
    )
    fixture_module = types.ModuleType("orchestrator.reboot_scope_fixture")
    fixture_module.__file__ = str(source)

    class RuntimeGeneration:
        def execute(self, request):
            return request

    RuntimeGeneration.__module__ = fixture_module.__name__
    fixture_module.RuntimeGeneration = RuntimeGeneration
    monkeypatch.setitem(sys.modules, fixture_module.__name__, fixture_module)

    monkeypatch.setattr(
        manager,
        "preflight_project_modules",
        lambda: [fixture_module.__name__],
    )
    reload_calls = []
    code_state = {"loaded": False}

    def reload_modules(module_names):
        reload_calls.append(tuple(module_names))
        code_state["loaded"] = True

    monkeypatch.setattr(
        manager,
        "reload_project_modules",
        reload_modules,
    )
    monkeypatch.setattr(manager, "validate_agent_runtime_contract", lambda: None)
    monkeypatch.setattr(manager, "rebuild_hot_managers", lambda: None)
    monkeypatch.setattr(
        "orchestrator.banner.show_startup_banner",
        lambda **_kwargs: None,
    )

    result = await manager.hot_restart(restart)

    assert result is True
    assert kernel.stop_calls == [
        (expected_target, f"hot-restart:{restart['mode']}")
    ]
    assert kernel.start_calls == [expected_target]
    assert reload_calls == [(fixture_module.__name__,)]
    assert sorted(runtime.name for runtime in kernel.runtimes) == ["sunny", "zelda"]
    assert untouched_runtime in kernel.runtimes
    assert kernel.service_manager.refreshed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "restart",
    [
        {"mode": "min", "agent_name": None},
        {"mode": "number", "agent_number": 99},
        {"mode": "unexpected", "agent_name": "zelda"},
    ],
)
async def test_invalid_hot_restart_scope_has_no_lifecycle_or_reload_side_effects(
    monkeypatch,
    restart,
):
    class Kernel:
        def __init__(self):
            self.runtimes = [
                SimpleNamespace(name="zelda"),
                SimpleNamespace(name="sunny"),
            ]
            self.stop_calls = []

        def configured_agent_names(self):
            return ["zelda", "sunny"]

        async def stop_agent(self, name, reason):
            self.stop_calls.append((name, reason))
            return True, "stopped"

    kernel = Kernel()
    manager = RebootManager(kernel=kernel, console_handler=None)
    monkeypatch.setattr(
        manager,
        "preflight_project_modules",
        lambda: pytest.fail("invalid scope must be rejected before source preflight"),
    )

    result = await manager.hot_restart(restart)

    assert result is False
    assert kernel.stop_calls == []


@pytest.mark.asyncio
async def test_hot_restart_reload_failure_restores_agent_and_requires_reboot_retry(
    monkeypatch,
    capsys,
):
    class ConsoleHandler:
        def addFilter(self, _filter):
            pass

        def removeFilter(self, _filter):
            pass

    class HotServices:
        def __init__(self):
            self.refreshed = False

        async def refresh_hot_services(self):
            self.refreshed = True

    class Kernel:
        def __init__(self):
            self.runtimes = [SimpleNamespace(name="arale")]
            self.whatsapp = None
            self.global_cfg = SimpleNamespace(workbench_port=18800)
            self.api_gateway = None
            self.service_manager = HotServices()
            self.start_calls = []

        async def stop_agent(self, name, reason):
            assert name == "arale"
            assert reason == "hot-restart:min"
            self.runtimes.clear()
            return True, "stopped"

        async def start_agent(self, name):
            self.start_calls.append(name)
            self.runtimes.append(SimpleNamespace(name=name))
            return True, "started"

        def _load_config_bundle(self):
            return None, [SimpleNamespace(name="arale")], None

    kernel = Kernel()
    manager = RebootManager(kernel=kernel, console_handler=ConsoleHandler())
    monkeypatch.setattr(manager, "preflight_project_modules", lambda: [])
    monkeypatch.setattr(
        manager,
        "reload_project_modules",
        lambda _names: (_ for _ in ()).throw(HotReloadError("ABI mismatch")),
    )
    monkeypatch.setattr(
        "orchestrator.banner.show_startup_banner",
        lambda **_kwargs: None,
    )

    result = await manager.hot_restart(
        {"mode": "min", "agent_name": "arale", "agent_number": None}
    )

    output = capsys.readouterr().out.casefold()
    assert result is False
    assert kernel.start_calls == ["arale"]
    assert [runtime.name for runtime in kernel.runtimes] == ["arale"]
    assert kernel.service_manager.refreshed is False
    assert "retry /reboot" in output
    assert "cold" not in output


@pytest.mark.asyncio
async def test_hot_restart_aborts_when_agent_stop_exceeds_deadline(
    monkeypatch,
    capsys,
):
    release_stop = asyncio.Event()
    stop_cancelled = asyncio.Event()

    class Kernel:
        def __init__(self):
            self.runtimes = [SimpleNamespace(name="samantha")]
            self.start_calls = []

        async def stop_agent(self, name, reason):
            assert name == "samantha"
            assert reason == "hot-restart:min"
            while not release_stop.is_set():
                try:
                    await release_stop.wait()
                except asyncio.CancelledError:
                    stop_cancelled.set()
            return True, "stopped"

        async def start_agent(self, name):
            self.start_calls.append(name)
            return True, "started"

    kernel = Kernel()
    manager = RebootManager(kernel=kernel, console_handler=None)
    monkeypatch.setattr(manager, "preflight_project_modules", lambda: [])
    monkeypatch.setattr(
        "orchestrator.reboot_manager.AGENT_STOP_TIMEOUT_SECONDS",
        0.01,
    )

    result = await asyncio.wait_for(
        manager.hot_restart(
            {"mode": "min", "agent_name": "samantha", "agent_number": None}
        ),
        timeout=0.5,
    )

    assert result is False
    output = capsys.readouterr().out.casefold()
    assert "retry /reboot" in output
    assert "cold" not in output
    await asyncio.sleep(0)
    assert stop_cancelled.is_set()
    assert kernel.start_calls == []
    release_stop.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_hot_restart_restores_only_agents_stopped_before_later_failure(monkeypatch):
    class Kernel:
        def __init__(self):
            self.runtimes = [
                SimpleNamespace(name="alpha"),
                SimpleNamespace(name="beta"),
            ]
            self.stop_calls = []
            self.start_calls = []

        async def stop_agent(self, name, reason):
            self.stop_calls.append((name, reason))
            if name == "alpha":
                self.runtimes[:] = [rt for rt in self.runtimes if rt.name != name]
                return True, "stopped"
            return False, "runtime is still active"

        async def start_agent(self, name):
            self.start_calls.append(name)
            self.runtimes.append(SimpleNamespace(name=name))
            return True, "started"

    kernel = Kernel()
    manager = RebootManager(kernel=kernel, console_handler=None)
    monkeypatch.setattr(manager, "preflight_project_modules", lambda: [])
    reload_calls = []
    monkeypatch.setattr(
        manager,
        "reload_project_modules",
        lambda _names: reload_calls.append(True),
    )

    result = await manager.hot_restart({"mode": "max"})

    assert result is False
    assert kernel.stop_calls == [
        ("alpha", "hot-restart:max"),
        ("beta", "hot-restart:max"),
    ]
    assert kernel.start_calls == ["alpha"]
    assert sorted(runtime.name for runtime in kernel.runtimes) == ["alpha", "beta"]
    assert reload_calls == []


@pytest.mark.asyncio
async def test_restart_delivery_health_watcher_replaces_existing_task(monkeypatch):
    started = []

    async def fake_watcher(kernel):
        started.append(kernel)
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "orchestrator.service_manager.delivery_health_watcher",
        fake_watcher,
    )
    kernel = SimpleNamespace(delivery_health_task=None)
    manager = ServiceManager(kernel)

    manager.start_delivery_health_watcher()
    first_task = kernel.delivery_health_task
    await asyncio.sleep(0)

    assert started == [kernel]
    assert first_task is not None
    assert not first_task.done()

    await manager.restart_delivery_health_watcher()
    second_task = kernel.delivery_health_task
    await asyncio.sleep(0)

    assert second_task is not first_task
    assert first_task.done()
    assert len(started) == 2

    await manager.stop_delivery_health_watcher()
    with suppress(asyncio.CancelledError):
        await second_task
