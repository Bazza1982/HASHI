from __future__ import annotations

import dataclasses
import os
import re
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from orchestrator.runtime_contract import enforce_runtime_contract
from orchestrator.function_generation import (
    CandidateProbeReceipt,
    FUNCTION_GENERATION_ENTRYPOINTS,
    FUNCTION_GENERATION_SCHEMA_VERSION,
    FunctionGenerationError,
    SourceManifest,
    build_source_manifest,
    candidate_import_guard,
    probe_function_generation,
    run_candidate_probe,
    verify_source_manifest,
)

ROOT = Path(__file__).resolve().parents[1]


def _source_module(name: str, source: Path) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = str(source)
    return module


def test_manifest_covers_lazy_imports_and_orders_provider_first(tmp_path):
    package = tmp_path / "orchestrator"
    package.mkdir()
    provider = package / "generation_provider.py"
    consumer = package / "generation_consumer.py"
    lazy = package / "generation_lazy.py"
    provider.write_text("VALUE = 42\n", encoding="utf-8")
    consumer.write_text(
        "from orchestrator.generation_provider import VALUE\n"
        "def later():\n"
        "    from orchestrator.generation_lazy import LAZY\n"
        "    return LAZY\n",
        encoding="utf-8",
    )
    lazy.write_text("LAZY = 7\n", encoding="utf-8")
    modules = {
        "orchestrator.generation_consumer": _source_module(
            "orchestrator.generation_consumer", consumer
        )
    }

    manifest = build_source_manifest(
        ("orchestrator.generation_consumer",),
        code_root=tmp_path,
        modules=modules,
    )

    assert set(manifest.module_names) == {
        "orchestrator.generation_provider",
        "orchestrator.generation_consumer",
        "orchestrator.generation_lazy",
    }
    assert manifest.module_names.index("orchestrator.generation_provider") < (
        manifest.module_names.index("orchestrator.generation_consumer")
    )
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", manifest.generation_id)


def test_manifest_rejects_invalid_source_before_worker_staging(tmp_path):
    package = tmp_path / "orchestrator"
    package.mkdir()
    source = package / "generation_broken.py"
    source.write_text("def broken(:\n    pass\n", encoding="utf-8")

    with pytest.raises(FunctionGenerationError, match="does not compile"):
        build_source_manifest(
            ["orchestrator.generation_broken"],
            code_root=tmp_path,
        )


def test_static_asset_is_content_addressed_and_runtime_output_is_excluded(tmp_path):
    package = tmp_path / "orchestrator"
    package.mkdir()
    (package / "generation_demo.py").write_text("VALUE = 1\n", encoding="utf-8")
    asset = package / "prompt_assets" / "system.txt"
    asset.parent.mkdir()
    asset.write_text("first", encoding="utf-8")
    run_output = tmp_path / "flow" / "runs" / "run-1" / "state.json"
    run_output.parent.mkdir(parents=True)
    run_output.write_text('{"mutable": true}', encoding="utf-8")

    first = build_source_manifest(
        ["orchestrator.generation_demo"],
        code_root=tmp_path,
    )
    run_output.write_text('{"mutable": false}', encoding="utf-8")
    unchanged = build_source_manifest(
        ["orchestrator.generation_demo"],
        code_root=tmp_path,
    )
    asset.write_text("second", encoding="utf-8")
    changed = build_source_manifest(
        ["orchestrator.generation_demo"],
        code_root=tmp_path,
    )

    assert first == unchanged
    assert first.generation_id != changed.generation_id
    assert [item.relative_path for item in first.assets] == [
        "orchestrator/prompt_assets/system.txt"
    ]


@pytest.mark.parametrize("changed", ["source", "asset"])
def test_verified_manifest_rejects_any_generation_byte_change(tmp_path, changed):
    package = tmp_path / "orchestrator"
    package.mkdir()
    source = package / "generation_changed.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    asset = package / "data.json"
    asset.write_text("{}", encoding="utf-8")
    manifest = build_source_manifest(
        ["orchestrator.generation_changed"],
        code_root=tmp_path,
    )
    if changed == "source":
        source.write_text("VALUE = 2\n", encoding="utf-8")
    else:
        asset.write_text('{"changed": true}', encoding="utf-8")

    with pytest.raises(FunctionGenerationError, match="source or asset changed"):
        verify_source_manifest(manifest, code_root=tmp_path)


def test_serialized_manifest_requires_current_schema(tmp_path):
    package = tmp_path / "orchestrator"
    package.mkdir()
    (package / "generation_demo.py").write_text("VALUE = 1\n", encoding="utf-8")
    manifest = build_source_manifest(
        ["orchestrator.generation_demo"],
        code_root=tmp_path,
    )

    assert SourceManifest.from_mapping(manifest.to_dict()) == manifest
    invalid = manifest.to_dict()
    invalid["schema_version"] = FUNCTION_GENERATION_SCHEMA_VERSION + 1
    with pytest.raises(FunctionGenerationError, match="schema"):
        SourceManifest.from_mapping(invalid)


def test_candidate_import_guard_blocks_probe_side_effects_but_not_live_thread(tmp_path):
    probe_output = tmp_path / "probe.txt"
    live_output = tmp_path / "live.txt"
    begin = threading.Event()
    complete = threading.Event()

    def live_writer():
        begin.wait(timeout=5)
        live_output.write_text("old Worker stayed live", encoding="utf-8")
        complete.set()

    thread = threading.Thread(target=live_writer)
    thread.start()
    try:
        with candidate_import_guard():
            begin.set()
            assert complete.wait(timeout=5)
            with pytest.raises(FunctionGenerationError, match="import-pure"):
                probe_output.write_text("forbidden", encoding="utf-8")
            with pytest.raises(FunctionGenerationError, match="process"):
                subprocess.run([sys.executable, "--version"], check=False)
    finally:
        thread.join(timeout=5)

    assert not probe_output.exists()
    assert live_output.read_text(encoding="utf-8") == "old Worker stayed live"


def test_function_generation_has_explicit_cold_core_entrypoints():
    assert {
        "orchestrator.flexible_agent_runtime",
        "orchestrator.admin_local_testing",
        "tools.registry",
        "transports.whatsapp",
    } <= set(FUNCTION_GENERATION_ENTRYPOINTS)
    assert "adapters.registry" not in FUNCTION_GENERATION_ENTRYPOINTS
    assert "orchestrator.telegram_delivery_failover" not in (
        FUNCTION_GENERATION_ENTRYPOINTS
    )


def test_default_hot_probe_does_not_seed_from_core_loaded_modules(monkeypatch):
    def reject_live_module_discovery(*_args, **_kwargs):
        raise AssertionError("Core live modules must not seed a Function generation")

    monkeypatch.setattr(
        "orchestrator.function_generation.discover_loaded_function_modules",
        reject_live_module_discovery,
    )
    seen = {}

    def probe_runner(*, code_root, module_names, expected_runtime):
        seen["module_names"] = tuple(module_names)
        manifest = build_source_manifest(module_names, code_root=code_root)
        return CandidateProbeReceipt(
            generation_id=manifest.generation_id,
            module_names=manifest.module_names,
            runtime=expected_runtime,
            probe_pid=os.getpid(),
        )

    kernel = types.SimpleNamespace(
        paths=types.SimpleNamespace(code_root=ROOT),
        runtime_fingerprint=enforce_runtime_contract(ROOT),
    )
    verified = probe_function_generation(kernel, probe_runner=probe_runner)

    assert set(FUNCTION_GENERATION_ENTRYPOINTS) <= set(seen["module_names"])
    assert "orchestrator.workbench_api" in verified.manifest.module_names


def test_in_process_generation_commit_api_is_retired():
    import orchestrator.function_generation as generation

    assert not hasattr(generation, "PreparedFunctionGeneration")
    assert not hasattr(generation, "prepare_function_generation")


def test_isolated_probe_rejects_dependency_environment_drift_before_import():
    incompatible = dataclasses.replace(
        enforce_runtime_contract(ROOT),
        dependency_digest="sha256:" + "0" * 64,
    )

    with pytest.raises(FunctionGenerationError, match="dependency_digest"):
        run_candidate_probe(
            code_root=ROOT,
            module_names=(),
            expected_runtime=incompatible,
            timeout_seconds=20,
        )


def test_generation_assets_do_not_include_local_runtime_state():
    manifest = build_source_manifest(
        ["orchestrator.flexible_agent_runtime"],
        code_root=ROOT,
    )

    assert manifest.assets
    assert all(not item.relative_path.startswith("flow/runs/") for item in manifest.assets)
    assert all("state/function_generations" not in item.relative_path for item in manifest.assets)
    assert os.path.isabs(str(ROOT))
