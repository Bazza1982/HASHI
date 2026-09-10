from __future__ import annotations

from types import SimpleNamespace

from orchestrator import manager_registry


def _kernel(tmp_path):
    return SimpleNamespace(
        paths=SimpleNamespace(
            code_root=tmp_path,
            bridge_home=tmp_path,
            tasks_path=tmp_path / "tasks.json",
        )
    )


def test_manager_registry_is_single_complete_manifest():
    attributes = [spec.attribute for spec in manager_registry.FUNCTION_MANAGER_SPECS]
    modules = [spec.module for spec in manager_registry.FUNCTION_MANAGER_SPECS]

    assert len(attributes) == len(set(attributes))
    assert set(attributes) == {
        "endpoint_registry",
        "capability_broker",
        "skill_manager",
        "config_admin",
        "backend_preflight",
        "agent_lifecycle",
        "agent_move_manager",
        "service_manager",
        "reboot_manager",
        "shutdown_manager",
        "startup_manager",
        "whatsapp_manager",
    }
    assert all(module.startswith("orchestrator.") for module in modules)


def test_core_manager_bundle_is_built_once_before_kernel_install(tmp_path):
    kernel = _kernel(tmp_path)
    created = []

    class FakeManager:
        def __init__(self, *args):
            created.append(args)

    module = SimpleNamespace()
    for spec in manager_registry.FUNCTION_MANAGER_SPECS:
        setattr(module, spec.class_name, FakeManager)

    bundle = manager_registry.build_function_manager_bundle(
        kernel,
        console_handler="console",
        module_loader=lambda _name: module,
    )

    assert not any(
        hasattr(kernel, spec.attribute)
        for spec in manager_registry.FUNCTION_MANAGER_SPECS
    )
    assert len(created) == len(manager_registry.FUNCTION_MANAGER_SPECS)

    manager_registry.install_function_manager_bundle(kernel, bundle)

    assert all(
        hasattr(kernel, spec.attribute)
        for spec in manager_registry.FUNCTION_MANAGER_SPECS
    )


def test_obsolete_hot_manager_api_is_not_exposed():
    assert not hasattr(manager_registry, "HOT_MANAGER_SPECS")
    assert not hasattr(manager_registry, "build_hot_manager_bundle")
    assert not hasattr(manager_registry, "install_hot_manager_bundle")
