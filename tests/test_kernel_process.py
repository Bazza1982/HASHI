from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from orchestrator.kernel_artifact import manifest_digest, verify_artifact
from orchestrator.kernel_process import KernelRuntime
from orchestrator.runtime_contract import enforce_runtime_contract

ROOT = Path(__file__).resolve().parents[1]
# A real subprocess peer exercises the same framed transport, artifact loader,
# process lifetime and rollback as the product; no provider or production state.
PEER = """
import asyncio, json, os
from pathlib import Path
from orchestrator.function_worker_protocol import JsonConnectionPeer
VALUE = {value!r}
FAILURE = {failure!r}
async def serve(connection, bootstrap):
    stopping = asyncio.Event()
    state = Path(bootstrap["bridge_home"]) / "progress.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    async def request(method, params):
        if method == "prepare":
            if FAILURE == "prepare": raise RuntimeError("prepare rejected")
            return {{"pid": os.getpid(), "generation_id": bootstrap["manifest"]["generation_id"]}}
        if method == "activate" and FAILURE == "activate": raise RuntimeError("activation rejected")
        if method == "quiesce" and FAILURE == "quiesce": raise RuntimeError("busy work")
        if method == "execute":
            count = json.loads(state.read_text()) if state.exists() else 0
            state.write_text(json.dumps(count + 1))
        if method == "stop": stopping.set()
        return {{"value": VALUE, "count": json.loads(state.read_text()) if state.exists() else 0}}
    peer = JsonConnectionPeer(connection, label="fixture", request_handler=request)
    peer.start()
    while not stopping.is_set() and not peer.is_closed:
        await asyncio.sleep(.01)
    await peer.close()
"""


def release(tmp_path, value="old", failure=""):
    artifact = tmp_path / (value + failure)
    module = artifact / "orchestrator" / "kernel_fixture.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(PEER.format(value=value, failure=failure))
    entries = [
        {
            "module": "orchestrator.kernel_fixture",
            "relative_path": "orchestrator/kernel_fixture.py",
            "sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
        }
    ]
    manifest = {
        "generation_id": manifest_digest(entries),
        "entries": entries,
        "assets": [],
        "schema_version": 2,
    }
    return {
        "generation_root": str(artifact),
        "manifest": manifest,
        "entrypoint": "orchestrator.kernel_fixture:serve",
    }


@pytest.mark.asyncio
async def test_real_process_adoption_preserves_kernel_and_durable_progress(tmp_path):
    kernel = KernelRuntime(ROOT, tmp_path / "instance", enforce_runtime_contract(ROOT))
    pid = os.getpid()
    await kernel.start(release(tmp_path))
    old = kernel.active.process.pid
    try:
        assert (await kernel.active.peer.request("execute"))["count"] == 1
        assert await kernel.replace(release(tmp_path, "new"))
        assert os.getpid() == pid
        assert kernel.active.process.pid != old
        result = await kernel.active.peer.request("inspect")
        assert result == {"value": "new", "count": 1}
    finally:
        await kernel.active.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["prepare", "activate", "tamper"])
async def test_rejected_candidate_restores_original_artifact_and_progress(
    tmp_path, failure
):
    kernel = KernelRuntime(ROOT, tmp_path / "instance", enforce_runtime_contract(ROOT))
    await kernel.start(release(tmp_path))
    old = kernel.active.process.pid
    candidate = release(tmp_path, "bad", failure)
    if failure == "tamper":
        source = Path(candidate["generation_root"]) / "orchestrator/kernel_fixture.py"
        source.write_text(
            source.read_text().replace("VALUE = 'bad'", "VALUE = 'tampered'")
        )
    try:
        await kernel.active.peer.request("execute")
        assert not await kernel.replace(candidate)
        if failure != "activate":
            assert kernel.active.process.pid == old
        assert await kernel.active.peer.request("inspect") == {
            "value": "old",
            "count": 1,
        }
    finally:
        await kernel.active.close()


@pytest.mark.asyncio
async def test_busy_old_process_is_not_terminated_for_rollout(tmp_path):
    kernel = KernelRuntime(ROOT, tmp_path / "instance", enforce_runtime_contract(ROOT))
    await kernel.start(release(tmp_path, failure="quiesce"))
    old = kernel.active.process.pid
    try:
        assert not await kernel.replace(release(tmp_path, "new"))
        assert kernel.active.process.pid == old
        assert (await kernel.active.peer.request("inspect"))["value"] == "old"
    finally:
        await kernel.active.close()


@pytest.mark.asyncio
async def test_entrypoint_import_is_guarded_before_product_code_runs(tmp_path):
    kernel = KernelRuntime(ROOT, tmp_path / "instance", enforce_runtime_contract(ROOT))
    await kernel.start(release(tmp_path))
    old = kernel.active.process.pid
    candidate = release(tmp_path, "unsafe")
    source = Path(candidate["generation_root"]) / "orchestrator/kernel_fixture.py"
    side_effect = tmp_path / "import-side-effect"
    source.write_text(
        source.read_text() + f"\nPath({str(side_effect)!r}).write_text('unsafe')\n"
    )
    candidate["manifest"]["entries"][0]["sha256"] = hashlib.sha256(
        source.read_bytes()
    ).hexdigest()
    candidate["manifest"]["generation_id"] = manifest_digest(
        candidate["manifest"]["entries"]
    )
    try:
        assert not await kernel.replace(candidate)
        assert not side_effect.exists()
        assert kernel.active.process.pid == old
        assert (await kernel.active.peer.request("inspect"))["value"] == "old"
    finally:
        await kernel.active.close()


def test_artifact_rejects_core_shadow_and_path_escape(tmp_path):
    for path in ("orchestrator/runtime_contract.py", "../escape.py"):
        entries = [
            {
                "module": "orchestrator.runtime_contract",
                "relative_path": path,
                "sha256": "0" * 64,
            }
        ]
        with pytest.raises(ValueError, match="invalid artifact path"):
            verify_artifact(
                tmp_path,
                {"entries": entries, "generation_id": manifest_digest(entries)},
            )


@pytest.mark.asyncio
async def test_real_product_generation_prepares_without_starting_live_services(
    tmp_path,
):
    from orchestrator.kernel_process import ProcessClient, qualify
    from orchestrator.pcm import render_pcm_document

    home = tmp_path / "isolated-instance"
    home.mkdir()
    workspace = home / "workspaces" / "offline_probe"
    workspace.mkdir(parents=True)
    (workspace / "agent.md").write_text(
        render_pcm_document(
            persona="Offline fixture", system="Validate local preparation only."
        )
    )
    (home / "agents.json").write_text(
        json.dumps(
            {
                "global": {"instance_id": "isolated-test"},
                "agents": [
                    {
                        "name": "offline_probe",
                        "type": "flex",
                        "is_active": False,
                        "workspace_dir": str(home / "workspaces" / "offline_probe"),
                        "active_backend": "codex-cli",
                        "allowed_backends": [{"engine": "codex-cli"}],
                    }
                ],
            }
        )
    )
    (home / "secrets.json").write_text(json.dumps({"placeholder": "offline-only"}))
    runtime = enforce_runtime_contract(ROOT)
    generation = await qualify(ROOT, home, runtime)
    modules = {item["module"] for item in generation["manifest"]["entries"]}
    assert {
        "orchestrator.runtime_app",
        "orchestrator.workbench_api",
        "orchestrator.api_gateway",
        "orchestrator.scheduler",
        "orchestrator.flexible_backend_registry",
    } <= modules
    kernel = KernelRuntime(ROOT, home, runtime)
    client = await ProcessClient.prepare(kernel.bootstrap(generation))
    try:
        assert client.process.is_alive()
        assert client.ready_metadata["project_root"] == str(ROOT)
        assert client.ready_metadata["bridge_home"] == str(home)
        assert not (home / "state" / "instance" / "process.pid").exists()
        assert not (home / "logs" / "orchestrator_state.json").exists()
    finally:
        await client.close()
    # A replacement with an empty config must reject before old intake is gated;
    # an initial cold launch must still expose the first-run setup flow.
    (home / "agents.json").write_text(json.dumps({"agents": []}))
    with pytest.raises(Exception, match="no configured Agents"):
        await ProcessClient.prepare(kernel.bootstrap(generation))
    initial = await ProcessClient.prepare(kernel.bootstrap(generation, initial=True))
    try:
        assert initial.ready_metadata["interactive"] is True
        assert not (home / "state" / "instance" / "process.pid").exists()
    finally:
        await initial.close()
