from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.function_worker_supervisor import FunctionWorkerSupervisor
from orchestrator.pathing import build_bridge_paths
from orchestrator.runtime_contract import enforce_runtime_contract
from orchestrator.runtime_release import qualify_release

ROOT = Path(__file__).resolve().parents[1]
FACTORY = "orchestrator.anatta.post_turn_observer:build_post_turn_observer"


def instance(tmp_path):
    home = tmp_path / "instance"
    workspace = home / "workspaces" / "observer"
    workspace.mkdir(parents=True)
    (home / "agents.json").write_text(json.dumps({"agents": [{
        "name": "observer", "workspace_dir": "workspaces/observer",
    }]}), encoding="utf-8-sig")
    return home, workspace


def declare(workspace, enabled):
    (workspace / "post_turn_observers.json").write_text(json.dumps({"observers": [
        {"factory": FACTORY, "enabled": enabled},
        {"factory": "orchestrator.missing_disabled:factory", "enabled": False},
    ]}), encoding="utf-8-sig")


def assert_observer_loads_from_artifact(home, workspace, manifest, artifact):
    request = home / "probe.json"
    request.write_text(json.dumps({"manifest": manifest, "artifact": str(artifact),
                                   "workspace": str(workspace), "source": str(ROOT)}))
    script = '''
import importlib, json, sys
from pathlib import Path
from orchestrator.kernel_artifact import GenerationModuleFinder
data=json.loads(Path(sys.argv[1]).read_text())
root=Path(data["artifact"])
sys.meta_path.insert(0, GenerationModuleFinder(generation_root=root,
    code_root=Path(data["source"]), manifest=data["manifest"]))
from orchestrator.post_turn_registry import build_post_turn_observers
from orchestrator.bridge_memory import BridgeMemoryStore
workspace=Path(data["workspace"])
observers=build_post_turn_observers(workspace_dir=workspace, bridge_memory_store=BridgeMemoryStore(workspace))
assert len(observers)==1, "Configured Anatta observer did not load"
assert observers[0].workspace_files_to_preserve()==frozenset({"anatta_config.json"})
module=importlib.import_module(type(observers[0]).__module__)
assert Path(module.__file__).is_relative_to(root)
'''
    result = subprocess.run([sys.executable, "-B", "-c", script, str(request)],
                            cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_cold_release_loads_declared_observer_inside_generation(tmp_path):
    home, workspace = instance(tmp_path)
    declare(workspace, True)
    release = qualify_release({"code_root": str(ROOT), "bridge_home": str(home),
        "runtime": enforce_runtime_contract(ROOT).to_dict(),
        "entrypoint": "orchestrator.runtime_app_host:run_runtime_app"})
    assert_observer_loads_from_artifact(home, workspace, release["manifest"], release["generation_root"])


@pytest.mark.asyncio
@pytest.mark.parametrize("disk", [False, True])
async def test_enabling_observer_rejects_incomplete_cached_generation(tmp_path, disk):
    home, workspace = instance(tmp_path)
    declare(workspace, False)
    kernel = SimpleNamespace(paths=build_bridge_paths(ROOT, home, canonical_home=True),
                             runtime_fingerprint=enforce_runtime_contract(ROOT))
    supervisor = FunctionWorkerSupervisor(kernel)
    old, _ = await supervisor.prepare_generation()
    declare(workspace, True)
    if disk:
        supervisor = FunctionWorkerSupervisor(kernel)
    generation, artifact = await supervisor.prepare_generation()
    assert generation.manifest.generation_id != old.manifest.generation_id
    assert_observer_loads_from_artifact(home, workspace, generation.manifest.to_dict(), artifact)
