from __future__ import annotations

import ast
import dataclasses
import os
import re
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

import main
from orchestrator import workspace_state as workspace_state_module
from orchestrator.function_generation import (
    CandidateProbeReceipt,
    FunctionGenerationError,
    _candidate_import_guard,
    build_source_manifest,
    prepare_function_generation,
    run_candidate_probe,
    validate_no_stale_bindings,
    verify_source_manifest,
)
from orchestrator.hot_reload import PROCESS_IDENTITY_MODULES
from orchestrator.pathing import build_bridge_paths

ROOT = Path(__file__).resolve().parents[1]


def _source_module(name: str, source: Path) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = str(source)
    return module


def test_manifest_orders_imported_provider_before_consumer(tmp_path):
    package = tmp_path / "orchestrator"
    package.mkdir()
    provider = package / "generation_provider.py"
    consumer = package / "generation_consumer.py"
    provider.write_text("VALUE = 42\n", encoding="utf-8")
    consumer.write_text(
        "from orchestrator.generation_provider import VALUE\n",
        encoding="utf-8",
    )
    modules = {
        "orchestrator.generation_consumer": _source_module(
            "orchestrator.generation_consumer", consumer
        ),
        "orchestrator.generation_provider": _source_module(
            "orchestrator.generation_provider", provider
        ),
    }

    manifest = build_source_manifest(
        tuple(modules),
        code_root=tmp_path,
        modules=modules,
    )

    assert manifest.module_names == (
        "orchestrator.generation_provider",
        "orchestrator.generation_consumer",
    )
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", manifest.generation_id)


def test_lazy_function_import_does_not_create_false_generation_cycle(tmp_path):
    package = tmp_path / "orchestrator"
    package.mkdir()
    provider = package / "generation_provider.py"
    consumer = package / "generation_admin.py"
    provider.write_text(
        "def resolve():\n"
        "    from orchestrator.generation_admin import VALUE\n"
        "    return VALUE\n",
        encoding="utf-8",
    )
    consumer.write_text(
        "from orchestrator.generation_provider import resolve\nVALUE = 42\n",
        encoding="utf-8",
    )
    modules = {
        "orchestrator.generation_admin": _source_module(
            "orchestrator.generation_admin", consumer
        ),
        "orchestrator.generation_provider": _source_module(
            "orchestrator.generation_provider", provider
        ),
    }

    manifest = build_source_manifest(
        ("orchestrator.generation_admin",),
        code_root=tmp_path,
        modules=modules,
    )

    assert manifest.module_names == (
        "orchestrator.generation_provider",
        "orchestrator.generation_admin",
    )


def test_manifest_detects_source_change_after_verification(tmp_path, monkeypatch):
    package = tmp_path / "orchestrator"
    package.mkdir()
    source = package / "generation_changed.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    name = "orchestrator.generation_changed"
    module = _source_module(name, source)
    monkeypatch.setitem(sys.modules, name, module)
    manifest = build_source_manifest([name], code_root=tmp_path)
    source.write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(FunctionGenerationError, match="changed after verification"):
        verify_source_manifest(manifest, code_root=tmp_path)


def test_manifest_rejects_invalid_source_before_candidate_probe(tmp_path):
    package = tmp_path / "orchestrator"
    package.mkdir()
    source = package / "generation_broken.py"
    source.write_text("def broken(:\n    pass\n", encoding="utf-8")
    name = "orchestrator.generation_broken"

    with pytest.raises(FunctionGenerationError, match="does not compile"):
        build_source_manifest(
            [name],
            code_root=tmp_path,
            modules={name: _source_module(name, source)},
        )


def test_stale_cross_generation_binding_is_rejected():
    old_provider = types.ModuleType("orchestrator.generation_provider")

    def old_function():
        return "old"

    old_function.__module__ = old_provider.__name__
    old_provider.public = old_function
    new_provider = types.ModuleType(old_provider.__name__)

    def new_function():
        return "new"

    new_function.__module__ = new_provider.__name__
    new_provider.public = new_function
    consumer = types.ModuleType("orchestrator.generation_consumer")
    consumer.public = old_function

    with pytest.raises(FunctionGenerationError, match="older module generation"):
        validate_no_stale_bindings(
            {
                new_provider.__name__: new_provider,
                consumer.__name__: consumer,
            }
        )


def test_candidate_import_guard_blocks_writes_and_restores_io(tmp_path):
    destination = tmp_path / "side-effect.txt"

    with pytest.raises(FunctionGenerationError, match="import-pure"):
        with _candidate_import_guard():
            destination.write_text("must not exist", encoding="utf-8")

    assert not destination.exists()
    destination.write_text("normal runtime", encoding="utf-8")
    assert destination.read_text(encoding="utf-8") == "normal runtime"


def test_candidate_guard_blocks_staging_process_launch_but_not_live_thread_io(
    tmp_path,
):
    destination = tmp_path / "live-generation-write.txt"
    begin = threading.Event()
    complete = threading.Event()

    def live_generation_worker():
        begin.wait(timeout=5)
        destination.write_text("live generation stayed operational", encoding="utf-8")
        complete.set()

    worker = threading.Thread(target=live_generation_worker)
    worker.start()
    try:
        with _candidate_import_guard():
            begin.set()
            assert complete.wait(timeout=5)
            with pytest.raises(FunctionGenerationError, match="subprocess.run"):
                subprocess.run([sys.executable, "--version"], check=False)
    finally:
        worker.join(timeout=5)

    assert destination.read_text(encoding="utf-8") == (
        "live generation stayed operational"
    )


def test_windows_sidecar_native_module_is_import_safe_on_core_platform():
    from tools.windows_helper import win32

    assert win32.AVAILABLE is (os.name == "nt")
    if os.name != "nt":
        with pytest.raises(RuntimeError, match="unavailable"):
            win32.list_windows()


def test_windows_sidecar_optional_dependencies_are_not_core_import_requirements():
    tree = ast.parse(
        (ROOT / "tools" / "windows_use_mcp_client.py").read_text(encoding="utf-8")
    )
    top_level_imports = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
    }

    assert not any(name == "fastmcp" or name.startswith("fastmcp.") for name in top_level_imports)


def test_manager_construction_is_inside_candidate_side_effect_guard(
    tmp_path,
    monkeypatch,
):
    paths = build_bridge_paths(ROOT, bridge_home=ROOT)
    kernel = main.UniversalOrchestrator(
        paths,
        selected_agents={"agent1"},
        enable_api_gateway=False,
    )
    destination = tmp_path / "manager-constructor-side-effect.txt"

    def accept_probe(*, code_root, module_names, expected_runtime):
        manifest = build_source_manifest(module_names, code_root=code_root)
        return CandidateProbeReceipt(
            generation_id=manifest.generation_id,
            module_names=manifest.module_names,
            runtime=expected_runtime,
            probe_pid=os.getpid() + 1,
        )

    def impure_constructor(*_args, **_kwargs):
        destination.write_text("must never happen", encoding="utf-8")

    monkeypatch.setattr(
        "orchestrator.manager_registry.build_hot_manager_bundle",
        impure_constructor,
    )

    with pytest.raises(FunctionGenerationError, match="import-pure"):
        prepare_function_generation(
            kernel,
            main._handler,
            probe_runner=accept_probe,
        )

    assert not destination.exists()


def test_isolated_probe_rejects_dependency_environment_drift():
    incompatible = dataclasses.replace(
        main.RUNTIME_FINGERPRINT,
        dependency_digest="sha256:" + "0" * 64,
    )

    with pytest.raises(FunctionGenerationError, match="dependency_digest"):
        run_candidate_probe(
            code_root=ROOT,
            module_names=(),
            expected_runtime=incompatible,
            timeout_seconds=20,
        )


@pytest.mark.integration
def test_full_function_surface_stages_activates_and_rolls_back_without_core_mutation(
    monkeypatch,
):
    paths = build_bridge_paths(ROOT, bridge_home=ROOT)
    kernel = main.UniversalOrchestrator(
        paths,
        selected_agents={"agent1"},
        enable_api_gateway=False,
    )
    old_runtime_module = sys.modules["orchestrator.runtime_common"]
    old_workspace_module = workspace_state_module
    shared_path = ROOT / "state" / "generation-lock-test.json"
    old_process_lock = old_workspace_module._path_lock(shared_path)
    old_core_modules = {
        name: sys.modules[name]
        for name in PROCESS_IDENTITY_MODULES
        if name in sys.modules
    }
    old_managers = {
        "agent_lifecycle": kernel.agent_lifecycle,
        "service_manager": kernel.service_manager,
        "reboot_manager": kernel.reboot_manager,
    }

    candidate = prepare_function_generation(kernel, main._handler)

    assert candidate.receipt.probe_pid != os.getpid()
    assert len(candidate.manifest.entries) >= 200
    assert "orchestrator.flexible_agent_runtime" in candidate.manifest.module_names
    assert "adapters.codex_cli" in candidate.manifest.module_names
    assert "tools.registry" in candidate.manifest.module_names
    assert not (set(candidate.manifest.module_names) & PROCESS_IDENTITY_MODULES)
    assert sys.modules["orchestrator.runtime_common"] is old_runtime_module
    assert candidate.active is False

    candidate.activate(kernel)

    assert candidate.active is True
    assert sys.modules["orchestrator.runtime_common"] is not old_runtime_module
    assert kernel.agent_lifecycle is not old_managers["agent_lifecycle"]
    assert kernel.service_manager is not old_managers["service_manager"]
    assert kernel.reboot_manager is not old_managers["reboot_manager"]
    assert all(sys.modules[name] is module for name, module in old_core_modules.items())
    assert kernel.function_generation["generation_id"] == candidate.manifest.generation_id
    assert (
        sys.modules["orchestrator.workspace_state"]._path_lock(shared_path)
        is old_process_lock
    )

    candidate.rollback(kernel)

    assert candidate.active is False
    assert sys.modules["orchestrator.runtime_common"] is old_runtime_module
    assert kernel.agent_lifecycle is old_managers["agent_lifecycle"]
    assert kernel.service_manager is old_managers["service_manager"]
    assert kernel.reboot_manager is old_managers["reboot_manager"]
    assert all(sys.modules[name] is module for name, module in old_core_modules.items())
    changed = dataclasses.replace(
        candidate.receipt.runtime,
        dependency_digest="sha256:" + "0" * 64,
    )
    monkeypatch.setattr(
        "orchestrator.function_generation.current_runtime_fingerprint",
        lambda *_args, **_kwargs: changed,
    )

    with pytest.raises(FunctionGenerationError, match="changed after candidate"):
        candidate.activate(kernel)

    assert candidate.active is False
    assert sys.modules["orchestrator.runtime_common"] is old_runtime_module
    assert kernel.agent_lifecycle is old_managers["agent_lifecycle"]
    assert kernel.service_manager is old_managers["service_manager"]
    assert kernel.reboot_manager is old_managers["reboot_manager"]
