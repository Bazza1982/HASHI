"""Permission boundaries use actual facts and retain failed cleanup ownership."""
import asyncio
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from adapters.base import TokenUsage
from adapters.her_v2_provider import HashiStageProvider
from orchestrator.permissions.host_usage import accept_host_usage
from orchestrator.permissions.models import PermissionLevel, normalize_scope
from orchestrator.permissions.native_factory import InstalledExecution, InstalledExecutorFactory
from orchestrator.permissions.resource_policy import ProtectedResourcePolicy
from orchestrator.runtime_contract import CORE_SOURCE_PATHS
from tools.meter_cost import PerCallUsageLineItem


def receiver():
    provider = HashiStageProvider.__new__(HashiStageProvider)
    provider.usage = TokenUsage()
    provider.usage_line_items = []
    provider.cost_usd = 0
    provider.tool_call_count = provider.tool_loop_count = 0
    provider.usage_observer = None
    return provider


def usage_response():
    return {
        "state": "parked", "accounting_epoch": "epoch",
        "usage": [asdict(PerCallUsageLineItem(
            request_id="call", phase="execution", engine="deepseek", model="deepseek-chat",
            provider_request_id="provider-id", input_tokens=999999, cost_usd=999,
        ))],
        "provider_evidence": [{
            "id": "service-call", "status": "completed", "engine": "deepseek",
            "model": "deepseek-chat", "provider_ids": ["provider-id"],
            "input_tokens": 23, "output_tokens": 7, "thinking_tokens": 0,
            "token_source": "provider", "cost_usd": .125,
            "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 23,
        }], "tool_call_count": 2, "tool_loop_count": 1,
    }


def test_parked_usage_uses_service_observations_and_transport_retry_does_not_double_count():
    provider, response = receiver(), usage_response()
    accept_host_usage(provider, response)
    accept_host_usage(provider, deepcopy(response))
    assert len(provider.usage_line_items) == 1
    assert provider.usage.input_tokens == 23 and provider.usage.output_tokens == 7
    assert provider.cost_usd == .125
    assert provider.tool_call_count == 2 and provider.tool_loop_count == 1
    response["provider_evidence"][0]["cost_usd"] = .25
    with pytest.raises(ValueError, match="changed"):
        accept_host_usage(provider, response)
    assert provider.cost_usd == .125


def test_usage_projection_validates_the_entire_batch_before_any_accounting_write():
    provider, response = receiver(), usage_response()
    response["provider_evidence"].append({
        **response["provider_evidence"][0], "id": "second", "input_tokens": -1,
    })
    with pytest.raises(ValueError, match="token"):
        accept_host_usage(provider, response)
    assert provider.usage_line_items == [] and provider.cost_usd == 0
    response = usage_response()
    response["provider_evidence"] = []
    with pytest.raises(ValueError, match="without service"):
        accept_host_usage(provider, response)


def test_live_core_private_and_service_paths_cannot_be_granted_by_a_broad_workspace(tmp_path):
    code, bridge, private, capsule, runtime = [tmp_path / name for name in (
        "code", "bridge", "private", "capsule", "runtime",
    )]
    for root in (code, bridge, private, capsule, runtime):
        root.mkdir()
    config = SimpleNamespace(
        project_root=code, bridge_home=bridge, secrets_path=bridge / "secrets.json",
        instance_id="TEST",
    )
    policy = ProtectedResourcePolicy(
        config, private_paths=(private,), immutable_paths=(capsule,), runtime_prefix=runtime,
    )
    def permits(path, action):
        return policy.permits(normalize_scope("file", str(path), (action,), cwd=tmp_path))
    assert permits(code / "ordinary.py", "write")
    assert permits(capsule / "module.py", "read")
    assert not permits(capsule / "module.py", "write")
    assert not permits(private / "keys.bin", "read")
    assert not permits(bridge / "secrets.json", "read")
    assert not permits(code / next(iter(CORE_SOURCE_PATHS)), "write")
    assert not permits(runtime / "Lib" / "module.py", "write")


@pytest.mark.asyncio
async def test_failed_executor_close_attempts_all_owners_and_is_retryable():
    calls = []
    class Endpoint:
        fail = True
        async def close(self):
            calls.append("endpoint")
            if self.fail:
                raise OSError("unverified pipe")
    endpoint = Endpoint()
    async def stop(binding):
        calls.append("host")
        return True
    async def close_channel():
        calls.append("channel")
        return True
    factory = InstalledExecutorFactory.__new__(InstalledExecutorFactory)
    factory.runner = SimpleNamespace(stop=stop)
    binding = SimpleNamespace(context=SimpleNamespace(execution_epoch="epoch"))
    execution = InstalledExecution(
        factory, binding, SimpleNamespace(settings=SimpleNamespace(requested_level=PermissionLevel.L2)),
        SimpleNamespace(registry=None, close=close_channel), endpoint, "private-pipe",
    )
    factory._owned = {"epoch": execution}
    with pytest.raises(OSError, match="unverified"):
        await factory.close()
    assert factory._owned["epoch"] is execution
    assert sorted(calls) == ["channel", "endpoint", "host"]
    endpoint.fail = False
    await factory.close()
    assert factory._owned == {} and execution._closed
    await asyncio.gather(execution.close(), execution.close())
    assert len(calls) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("successor", [False, True])
async def test_successor_requires_its_own_pending_checkpoint_before_tool_admission(successor):
    checkpoint = {"boundary": "pending_tool_batch", "messages": []}
    if successor:
        checkpoint["_herv3_continuation"] = {
            "successor_turn": True, "resume_pending_tools": False, "pcm_delta": [],
        }
    seeded, received = [], []

    class Callbacks:
        async def recovery(self, _binding):
            return checkpoint

    class Runner:
        async def run_her(self, _execution, command):
            received.append(command["recovery"])
            return {"state": "completed"}

    factory = SimpleNamespace(runner=Runner())
    models = SimpleNamespace(usage_receipts=lambda: [])
    channel = SimpleNamespace(registry=None, _models=models, _completed_tool_calls=set(),
                              seed_trusted_recovery=seeded.append)
    binding = SimpleNamespace(context=SimpleNamespace(execution_epoch="epoch"))
    policy = SimpleNamespace(settings=SimpleNamespace(requested_level=PermissionLevel.L2))
    execution = InstalledExecution(factory, binding, policy, channel, None, "private")
    execution._callbacks = Callbacks()
    await execution.run_restricted_her({})
    assert received == [checkpoint]
    assert seeded == ([None] if successor else [checkpoint])
# HASHI-Fingerprint: hfp_3eeb5952b98f4d1084a55e633c0050d7
