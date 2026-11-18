"""Shared handoff must retain the retiring personal data binding."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from orchestrator import personal_data_owner, runtime_handoff
from orchestrator.config import ConfigManager
from orchestrator.config_json import read_config_json
from orchestrator.runtime_app_host import RuntimeAppHost
from tests.test_config import _minimal_flex_agent, _write_base_files


@pytest.fixture
def instance(tmp_path, monkeypatch):
    monkeypatch.delenv("HASHI_REMOTE_SHARED_TOKEN", raising=False)
    monkeypatch.delenv("HASHI_REMOTE_ROOT", raising=False)
    config, secrets = _write_base_files(tmp_path, _minimal_flex_agent())
    data = json.loads(config.read_text(encoding="utf-8"))
    data["global"]["owner_scope_id"] = "user:disk"
    config.write_text(json.dumps(data), encoding="utf-8")
    secrets.write_text(json.dumps({"workbench_admin_token":"local-fixture",
                                   "hashi_remote_shared_token":"network-fixture"}), encoding="utf-8")
    return config, secrets


def test_candidate_config_load_does_not_establish_owner_before_handoff(instance, tmp_path):
    config, secrets = instance
    cfg, _, _ = ConfigManager(config, secrets, bridge_home=tmp_path).load(
        bootstrap_local_api_credential=True, defer_personal_data_binding=True,
    )
    assert cfg.owner_scope_id == "user:disk"
    assert personal_data_owner.read_personal_data_binding(tmp_path) is None
    assert read_config_json(config)["global"]["owner_scope_id"] == "user:disk"


def test_handoff_carries_the_retiring_runtime_owner_without_creating_a_binding(tmp_path):
    app = SimpleNamespace(
        paths=SimpleNamespace(bridge_home=tmp_path),
        global_cfg=SimpleNamespace(deployment_profile="personal", owner_scope_id="user:retiring"),
        runtimes=[], function_workers=SimpleNamespace(_telegram_ingress={}),
    )
    result = runtime_handoff.snapshot(app)
    assert result["personal_data_binding"] == {
        "version":1, "data_owner_id":"user:retiring", "legacy_owner_known":False,
    }
    assert personal_data_owner.read_personal_data_binding(tmp_path) is None


def test_handoff_binding_never_overwrites_a_different_existing_binding(tmp_path):
    personal_data_owner.resolve_personal_data_owner(
        tmp_path, {"owner_scope_id":"user:existing"}, None, establish=True,
    )
    path = tmp_path / "state" / "personal_data_owner.json"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="binding"):
        personal_data_owner.establish_handoff_data_binding(tmp_path, {
            "version":1, "data_owner_id":"user:other", "legacy_owner_known":True,
        })
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_candidate_uses_retiring_owner_even_if_disk_config_changed(instance, tmp_path):
    config, secrets = instance
    manager = ConfigManager(config, secrets, bridge_home=tmp_path)
    candidate, _, _ = manager.load(
        bootstrap_local_api_credential=True, defer_personal_data_binding=True,
    )
    assert candidate.owner_scope_id == "user:disk"
    loads = []

    def load_config_bundle(*, bootstrap_local_api_credential=False, **kwargs):
        cfg, _, _ = manager.load(bootstrap_local_api_credential=bootstrap_local_api_credential,
                                 **kwargs)
        app.global_cfg = cfg
        loads.append(cfg.owner_scope_id)
        return cfg, [], {}

    async def run():
        await asyncio.Event().wait()

    app = SimpleNamespace(
        paths=SimpleNamespace(bridge_home=tmp_path), global_cfg=candidate,
        _shared_replacement_candidate=True, _load_config_bundle=load_config_bundle,
        run=run, startup_status={"services_ready":True, "phase":"ready"},
        runtimes=[], agent_lifecycle=SimpleNamespace(manually_stopped_agents=set()),
    )
    host = RuntimeAppHost(None, {})
    host.app = app
    host._candidate_personal_binding_deferred = True
    handoff = {"personal_data_binding":{
        "version":1, "data_owner_id":"user:retiring", "legacy_owner_known":False,
    }}
    await host.activate(handoff)
    try:
        assert personal_data_owner.read_personal_data_binding(tmp_path) == ("user:retiring", False)
        assert loads == ["user:retiring"]
        assert app.global_cfg.owner_scope_id == "user:retiring"
        assert read_config_json(config)["global"]["owner_scope_id"] == "user:disk"
    finally:
        host.task.cancel()
        await asyncio.gather(host.task, return_exceptions=True)



def test_snapshot_refuses_a_runtime_owner_that_disagrees_with_persisted_binding(tmp_path):
    personal_data_owner.resolve_personal_data_owner(
        tmp_path, {"owner_scope_id":"user:persisted"}, None, establish=True,
    )
    binding_path = tmp_path / "state" / "personal_data_owner.json"
    before = binding_path.read_bytes()
    app = SimpleNamespace(
        paths=SimpleNamespace(bridge_home=tmp_path),
        global_cfg=SimpleNamespace(deployment_profile="personal", owner_scope_id="user:other"),
        runtimes=[], function_workers=SimpleNamespace(_telegram_ingress={}),
    )
    with pytest.raises(ValueError, match="differs"):
        runtime_handoff.snapshot(app)
    assert binding_path.read_bytes() == before


def test_governed_snapshot_does_not_invent_a_personal_data_binding(tmp_path):
    app = SimpleNamespace(
        paths=SimpleNamespace(bridge_home=tmp_path),
        global_cfg=SimpleNamespace(deployment_profile="enterprise", owner_scope_id="user:governed"),
        runtimes=[], function_workers=SimpleNamespace(_telegram_ingress={}),
    )
    assert "personal_data_binding" not in runtime_handoff.snapshot(app)
    assert personal_data_owner.read_personal_data_binding(tmp_path) is None


@pytest.mark.asyncio
async def test_legacy_handoff_keeps_existing_fallback_available(instance, tmp_path):
    config, secrets = instance
    manager = ConfigManager(config, secrets, bridge_home=tmp_path)
    candidate, _, _ = manager.load(
        bootstrap_local_api_credential=True, defer_personal_data_binding=True,
    )

    def load_config_bundle(*, bootstrap_local_api_credential=False, **kwargs):
        cfg, _, _ = manager.load(bootstrap_local_api_credential=bootstrap_local_api_credential,
                                 **kwargs)
        app.global_cfg = cfg
        return cfg, [], {}

    async def run():
        await asyncio.Event().wait()

    app = SimpleNamespace(
        paths=SimpleNamespace(bridge_home=tmp_path), global_cfg=candidate,
        _shared_replacement_candidate=True, _load_config_bundle=load_config_bundle,
        run=run, startup_status={"services_ready":True, "phase":"ready"},
        runtimes=[], agent_lifecycle=SimpleNamespace(manually_stopped_agents=set()),
    )
    host = RuntimeAppHost(None, {})
    host.app = app
    host._candidate_personal_binding_deferred = True
    await host.activate({"agents":[]})
    try:
        assert personal_data_owner.read_personal_data_binding(tmp_path) == ("user:disk", True)
    finally:
        host.task.cancel()
        await asyncio.gather(host.task, return_exceptions=True)



@pytest.mark.asyncio
async def test_real_host_prepare_waits_for_retiring_owner_snapshot(instance, tmp_path, monkeypatch):
    from contextlib import nullcontext

    from orchestrator import bootstrap_logging, function_worker_supervisor, onboarding_gate
    from orchestrator import pathing, runtime_app, runtime_app_host

    config, secrets = instance
    manager = ConfigManager(config, secrets, bridge_home=tmp_path)
    loaded = []

    async def run():
        await asyncio.Event().wait()

    def load_bundle(**kwargs):
        cfg, agents, values = manager.load(**kwargs)
        app.global_cfg = cfg
        loaded.append(cfg.owner_scope_id)
        return cfg, agents, values

    app = SimpleNamespace(
        paths=SimpleNamespace(bridge_home=tmp_path),
        runtime_fingerprint=SimpleNamespace(runtime_id="runtime-fixture"),
        shutdown_manager=SimpleNamespace(start_exit_watchdog=lambda: None),
        _load_config_bundle=load_bundle, run=run,
        startup_status={"services_ready":True, "phase":"ready"},
        runtimes=[], agent_lifecycle=SimpleNamespace(manually_stopped_agents=set()),
    )
    monkeypatch.setattr(runtime_app_host, "validate_artifact_identity", lambda *_args: None)
    monkeypatch.setattr(runtime_app_host, "candidate_import_guard", nullcontext)
    monkeypatch.setattr(runtime_app_host, "validate_function_contract", lambda: None)
    monkeypatch.setattr(pathing, "build_bridge_paths", lambda *_args, **_kwargs: app.paths)
    monkeypatch.setattr(runtime_app, "UniversalOrchestrator", lambda *_args, **_kwargs: app)
    monkeypatch.setattr(bootstrap_logging, "configure_terminal_console", lambda *_args: None)
    monkeypatch.setattr(onboarding_gate, "needs_onboarding", lambda *_args: False)
    monkeypatch.setattr(function_worker_supervisor, "generation_artifact_source_commit",
                        lambda *_args: "a" * 40)
    monkeypatch.setattr(function_worker_supervisor, "generation_from_dict",
                        lambda value: SimpleNamespace(value=value))

    host = RuntimeAppHost(None, {
        "manifest":{"generation_id":"fixture", "entries":[]},
        "generation_root":str(tmp_path), "code_root":str(tmp_path),
        "bridge_home":str(tmp_path), "runtime":{}, "source_commit":"a" * 40,
        "kernel_pid":123, "arguments":{},
    })
    await host.prepare()
    assert loaded == ["user:disk"]
    assert personal_data_owner.read_personal_data_binding(tmp_path) is None

    old = SimpleNamespace(
        paths=SimpleNamespace(bridge_home=tmp_path),
        global_cfg=SimpleNamespace(deployment_profile="personal", owner_scope_id="user:retiring"),
        runtimes=[], function_workers=SimpleNamespace(_telegram_ingress={}),
    )
    await host.activate(runtime_handoff.snapshot(old))
    try:
        assert loaded == ["user:disk", "user:retiring"]
        assert app.global_cfg.owner_scope_id == "user:retiring"
        assert personal_data_owner.read_personal_data_binding(tmp_path) == ("user:retiring", False)
    finally:
        host.task.cancel()
        await asyncio.gather(host.task, return_exceptions=True)



@pytest.mark.asyncio
async def test_personal_handoff_does_not_bind_a_governed_candidate(tmp_path):
    app = SimpleNamespace(
        paths=SimpleNamespace(bridge_home=tmp_path),
        global_cfg=SimpleNamespace(deployment_profile="enterprise"),
        _load_config_bundle=lambda **_kwargs: pytest.fail("unexpected configuration reload"),
        agent_lifecycle=SimpleNamespace(manually_stopped_agents=set()),
    )
    host = RuntimeAppHost(None, {})
    host.app = app
    host._candidate_personal_binding_deferred = True
    with pytest.raises(ValueError, match="governed"):
        await host.activate({"personal_data_binding":{
            "version":1, "data_owner_id":"user:retiring", "legacy_owner_known":True,
        }})
    assert host.task is None
    assert personal_data_owner.read_personal_data_binding(tmp_path) is None


def test_concurrent_handoff_bindings_never_overwrite_the_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    def establish(owner):
        try:
            return personal_data_owner.establish_handoff_data_binding(tmp_path, {
                "version":1, "data_owner_id":owner, "legacy_owner_known":True,
            })
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(establish, ["user:first", "user:second"]))
    assert results.count("conflict") == 1
    winner = next(value for value in results if value != "conflict")
    assert personal_data_owner.read_personal_data_binding(tmp_path) == (winner, True)



def test_snapshot_detects_disagreement_between_runtime_and_live_session_api(tmp_path):
    app = SimpleNamespace(
        paths=SimpleNamespace(bridge_home=tmp_path),
        global_cfg=SimpleNamespace(deployment_profile="personal", owner_scope_id="user:reloaded"),
        workbench_api=SimpleNamespace(global_config=SimpleNamespace(
            deployment_profile="personal", owner_scope_id="user:session-api",
        )),
        runtimes=[], function_workers=SimpleNamespace(_telegram_ingress={}),
    )
    with pytest.raises(ValueError, match="session API"):
        runtime_handoff.snapshot(app)
    assert personal_data_owner.read_personal_data_binding(tmp_path) is None


def test_snapshot_accepts_matching_live_session_api_and_runtime_owner(tmp_path):
    cfg = SimpleNamespace(deployment_profile="personal", owner_scope_id="user:actual")
    app = SimpleNamespace(
        paths=SimpleNamespace(bridge_home=tmp_path), global_cfg=cfg,
        workbench_api=SimpleNamespace(global_config=cfg),
        runtimes=[], function_workers=SimpleNamespace(_telegram_ingress={}),
    )
    assert runtime_handoff.snapshot(app)["personal_data_binding"]["data_owner_id"] == "user:actual"
