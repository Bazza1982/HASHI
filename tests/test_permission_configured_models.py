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
            self.config = SimpleNamespace(system_md=tmp_path / "private-agent.md")
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
