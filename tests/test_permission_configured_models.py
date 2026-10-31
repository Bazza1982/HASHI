"""Freeze qualified model profiles without exposing private transport settings."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from orchestrator.her_v2.config import HERv2Config
from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.configured_models import ConfiguredModelFactory
from orchestrator.permissions.pao_run_fence import PaoRunFence
from orchestrator.permissions.models import RuntimeExecutionBinding
from tests.test_permission_pao_fence import setup_run


@pytest.mark.parametrize("bad_second_profile", [False, True])
@pytest.mark.asyncio
async def test_qualified_public_profiles_and_partial_initialization_cleanup(
    tmp_path, monkeypatch, bad_second_profile,
):
    store, _, accepted, fence, context = setup_run(tmp_path)
    binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")
    guard = PaoRunFence(store, context=context, request_id=accepted.request_id,
                        fencing_token=fence, worker_id="INSTANCE:agent")
    config = HERv2Config.from_mapping({"profiles": {
        name: {"engine": "deepseek-api", "model": "deepseek-v4-pro"}
        for name in ("lightweight", "triage", "premium", "reviewer", "orchestrator")
    }})
    good = config.all_provider_profiles()[0]
    bad = replace(good, name="other", model="other-model", options={"api_key": "never-export-this"})
    monkeypatch.setattr(HERv2Config, "all_provider_profiles", lambda self: (
        (good, bad) if bad_second_profile else (good,)
    ))
    created = []

    class Driver:
        def __init__(self):
            self.config = SimpleNamespace(system_md=tmp_path / "private-agent.md", extra={})
            self.closed = False

        async def initialize(self):
            assert self.config.workspace_dir == tmp_path and self.config.system_md is None
            return True

        async def shutdown(self):
            self.closed = True

    class Manager:
        privacy_level = 1

        def __init__(self, agent, global_config, secrets):
            assert secrets["model-key"] == "never-export-this"

        def create_herv3_provider_backend(self, engine, model):
            assert (engine, model) == (good.engine, good.model)
            driver = Driver()
            created.append(driver)
            return driver

    monkeypatch.setattr("orchestrator.permissions.configured_models.FlexibleBackendManager", Manager)
    source = SimpleNamespace(load=lambda: (
        SimpleNamespace(), [SimpleNamespace(name=context.agent_id)], {"model-key": "never-export-this"},
    ))
    factory = ConfiguredModelFactory(source, object(), private_workspace=tmp_path)
    if bad_second_profile:
        with pytest.raises(PermissionDenied):
            await factory.create(binding, config, guard)
        assert created and all(driver.closed for driver in created)
    else:
        broker, public = await factory.create(binding, config, guard)
        assert public["privacy_level"] == 1 and len(public["profiles"]) == 1
        assert "never-export-this" not in repr(public)
        await broker.close()
        assert all(driver.closed for driver in created)


@pytest.mark.parametrize("has_key", [False, True])
@pytest.mark.asyncio
async def test_installed_factory_uses_real_adapter_timeout_and_lifecycle(tmp_path, monkeypatch, has_key):
    from adapters.deepseek_api import DeepSeekAdapter
    from orchestrator.config import FlexibleAgentConfig, GlobalConfig
    from orchestrator.flexible_backend_manager import FlexibleBackendManager

    store, _, accepted, fence, context = setup_run(tmp_path)
    binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")
    guard = PaoRunFence(store, context=context, request_id=accepted.request_id,
                        fencing_token=fence, worker_id="INSTANCE:agent")
    workspace = tmp_path / "agent"
    workspace.mkdir()
    agent = FlexibleAgentConfig(
        name=context.agent_id, workspace_dir=workspace, system_md=workspace / "agent.md",
        telegram_token_key="unused", active_backend="her-v2", project_root=workspace,
        allowed_backends=[
            {"engine": "her-v2", "model": "deepseek-flash"},
            {"engine": "deepseek-api", "models": ["deepseek-flash"],
             "api_key_secret": "model-key", "temperature": 0.35, "idle_timeout_sec": 9},
        ],
    )
    global_config = GlobalConfig(authorized_id=1, project_root=workspace)
    source = SimpleNamespace(load=lambda: (
        global_config, [agent], {"model-key": "only-a-test-key"} if has_key else {},
    ))
    config = HERv2Config.from_mapping({
        "user_idle_timeout_s": 127,
        "profiles": {
            name: {"engine": "deepseek-api", "model": "deepseek-flash"}
            for name in ("lightweight", "triage", "premium", "reviewer", "orchestrator")
        },
    })
    created = []
    create_provider = FlexibleBackendManager.create_herv3_provider_backend

    def observe_provider(manager, *args):
        driver = create_provider(manager, *args)
        created.append(driver)
        return driver

    monkeypatch.setattr(FlexibleBackendManager, "create_herv3_provider_backend", observe_provider)
    private_workspace = tmp_path / "private"
    factory = ConfiguredModelFactory(source, object(), private_workspace=private_workspace)
    if has_key:
        broker, public = await factory.create(binding, config, guard)
        assert len(created) == 1
        driver = created[0]
        assert isinstance(driver, DeepSeekAdapter)
        assert driver.IDLE_TIMEOUT_SEC == 127
        assert driver.config.extra["temperature"] == 0.35
        assert driver.config.workspace_dir == private_workspace
        assert driver.config.system_md is None
        assert driver.client is not None and not driver.client.is_closed
        assert "only-a-test-key" not in repr(public)
        client = driver.client
        await broker.close()
        assert client.is_closed and driver.client is None
    else:
        with pytest.raises(PermissionDenied, match="could not initialize"):
            await factory.create(binding, config, guard)
        assert len(created) == 1 and created[0].IDLE_TIMEOUT_SEC == 127
        assert created[0].client is None
