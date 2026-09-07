from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import runtime_handoff
from orchestrator.function_generation import (
    CandidateProbeReceipt,
    VerifiedFunctionGeneration,
    build_source_manifest,
)
from orchestrator.function_worker_supervisor import materialize_generation_artifact
from orchestrator.runtime_contract import enforce_runtime_contract

ROOT = Path(__file__).resolve().parents[1]


def generation(tmp_path, home, marker, runtime):
    source = tmp_path / marker
    (source / "orchestrator").mkdir(parents=True)
    (source / "orchestrator" / "example.py").write_text(f"VALUE = {marker!r}\n")
    manifest = build_source_manifest(["orchestrator.example"], code_root=source)
    result = VerifiedFunctionGeneration(
        source,
        manifest,
        CandidateProbeReceipt(
            manifest.generation_id, manifest.module_names, runtime, 123
        ),
    )
    artifact = materialize_generation_artifact(home, result)
    return SimpleNamespace(
        generation=result,
        generation_root=artifact,
        generation_id=manifest.generation_id,
    )


def test_recovery_retains_mixed_agent_versions_selected_set_and_latest_offsets(
    tmp_path,
):
    home = tmp_path / "instance"
    runtime = enforce_runtime_contract(ROOT)
    old = generation(tmp_path, home, "old", runtime)
    updated = generation(tmp_path, home, "updated", runtime)
    app = SimpleNamespace(
        paths=SimpleNamespace(bridge_home=home),
        runtime_fingerprint=runtime,
        _shared_committed=True,
        shared_generation_id=old.generation_id,
        runtimes=[
            SimpleNamespace(name="alpha", client=updated),
            SimpleNamespace(name="beta", client=old),
        ],
        function_workers=SimpleNamespace(
            _telegram_ingress={"alpha": SimpleNamespace(offset=7)}
        ),
    )
    handoff = runtime_handoff.snapshot(app)
    restored = runtime_handoff.agent_artifacts(app, handoff)
    assert restored["alpha"][0].manifest == updated.generation.manifest
    assert restored["beta"][0].manifest == old.generation.manifest
    # A manual stop is persisted separately from the original CLI/config set.
    app.runtimes.pop()
    runtime_handoff.persist(app)
    runtime_handoff.checkpoint_offset(home, "alpha", 42)
    app.runtimes.clear()  # the shared process has disappeared
    recovered = runtime_handoff.load(app)
    assert recovered["agents"] == ["alpha"]
    assert recovered["telegram_offsets"] == {"alpha": 42}
    assert (
        runtime_handoff.agent_artifacts(app, recovered)["alpha"][0].manifest
        == updated.generation.manifest
    )
    handoff["generations"][updated.generation_id]["root"] = str(
        tmp_path / "other-instance"
    )
    with pytest.raises(ValueError, match="outside"):
        runtime_handoff.agent_artifacts(app, handoff)


@pytest.mark.asyncio
async def test_transient_connector_failure_retries_without_blocking_other_connectors(
    tmp_path, monkeypatch
):
    from orchestrator.runtime_app_host import RuntimeAppHost

    monkeypatch.setattr("orchestrator.runtime_app_host.CONNECTOR_RETRY_SECONDS", 0.01)

    class Ingress:
        offset = None

        def __init__(self, fail=False):
            self.calls = 0
            self.is_running = False
            self.fail = fail

        async def start(self, **kwargs):
            self.calls += 1
            if self.fail and self.calls == 1:
                raise OSError("temporary connection error")
            self.is_running = True

    flaky, healthy = Ingress(True), Ingress()
    whatsapp_calls = []

    async def start_whatsapp_transport(**kwargs):
        whatsapp_calls.append(True)
        app.whatsapp = object()
        return True, "started"

    app = SimpleNamespace(
        paths=SimpleNamespace(bridge_home=tmp_path),
        runtimes=[],
        shared_generation_id="generation-a",
        _handoff_draining=True,
        startup_status={"issues": []},
        api_gateway=None,
        whatsapp=None,
        function_workers=SimpleNamespace(
            _telegram_ingress={"alpha": flaky, "beta": healthy}
        ),
        global_cfg=SimpleNamespace(authorized_id=1),
        _runtime_map=lambda: {},
        _load_whatsapp_cfg=lambda: ({}, {"enabled": True}),
        start_whatsapp_transport=start_whatsapp_transport,
    )
    host = RuntimeAppHost(None, {})
    host.app = app
    host.task = asyncio.create_task(asyncio.Event().wait())
    try:
        result = await host.commit()
        assert result["committed"] and result["degraded"]
        assert healthy.is_running and whatsapp_calls
        await asyncio.wait_for(host.connector_task, timeout=1)
        assert flaky.is_running and flaky.calls == 2
        assert app.startup_status["ready"] and not app.startup_status["degraded"]
    finally:
        host.stopping.set()
        host.task.cancel()
        await asyncio.gather(host.task, return_exceptions=True)
        if host.connector_task:
            host.connector_task.cancel()
            await asyncio.gather(host.connector_task, return_exceptions=True)


def test_core_and_function_paths_share_one_canonical_instance_identity(
    tmp_path, monkeypatch
):
    from orchestrator.kernel_process import canonical_instance_home
    from orchestrator.pathing import build_bridge_paths

    monkeypatch.setenv("HASHI_SCOPE_TEST", "production")
    raw = str(tmp_path / "$HASHI_SCOPE_TEST") + '" --ignored'
    canonical = canonical_instance_home(ROOT, raw)
    assert canonical == tmp_path / "production"
    assert build_bridge_paths(ROOT, raw).bridge_home == canonical
    literal = tmp_path / "$HASHI_SCOPE_TEST"
    assert build_bridge_paths(ROOT, literal, canonical_home=True).bridge_home == literal
