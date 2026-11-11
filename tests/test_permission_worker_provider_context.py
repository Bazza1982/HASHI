"""Real Worker StageProvider context survives only on the owning execution path."""
from __future__ import annotations

import base64
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from adapters.her_v2_provider import HashiStageProvider
from orchestrator.permissions.clients import RemoteRunExecutor
from orchestrator.permissions.host_protocol import unpack
from orchestrator.permissions.models import RuntimeExecutionBinding
from tests.test_permission_pao_fence import setup_run
from tests.test_permission_restricted_her import command


class BoundaryObserved(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("level,resuming", [("L2", False), ("L2", True), ("L1", True)])
async def test_worker_uses_real_provider_context_contract_at_host_handoff(tmp_path, level, resuming):
    _, _, accepted, fence, context = setup_run(tmp_path)
    binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")
    calls = []

    class Client:
        def call(self, method, arguments):
            assert method == "worker_usage_receipts"
            return []

        async def call_async(self, method, arguments, **options):
            calls.append((method, arguments))
            if method == "configure_inference":
                return {"profiles": payload["profiles"], "privacy_level": 1}
            assert method == "run_her"
            raise BoundaryObserved("restricted host handoff")

    owner = SimpleNamespace(_client=Client(), workspace_dir=tmp_path)
    key = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    executor = RemoteRunExecutor(owner, binding, {
        "binding": binding, "level": level, "tool_definitions": [],
        "endpoint": r"\\.\pipe\LOCAL\HASHI.Permissions.worker_context_test",
        "public_key": base64.b64encode(key).decode(),
    })
    payload = command(executor.registry)
    config = unpack(payload["config"])
    provider = HashiStageProvider(backend_manager=SimpleNamespace())
    original_observer = lambda checkpoint: None
    recovery = {"messages": [{"role": "user", "content": "original pending context"}]} if resuming else None
    provider.bind_provider_context(observer=original_observer, recovery=recovery)
    executor.bind_provider(provider)

    async def local_turn(*args, **kwargs):
        calls.append(("local_turn", kwargs))
        raise BoundaryObserved("original L1 execution")

    audit = SimpleNamespace(for_execution_epoch=lambda epoch: audit)
    runtime = SimpleNamespace(config=config, pcm_input={}, skills_catalogue=[],
                              delivery=SimpleNamespace(callback=None), audit_log=audit,
                              run_turn=local_turn)
    with pytest.raises(BoundaryObserved):
        await executor.run_her(runtime, "original prompt", binding.request_id,
                               binding=binding, permission_recovery=recovery)
    if level == "L2":
        assert provider._provider_context_observer is None
        assert provider._provider_context_recovery == {}
        assert calls[-1][0] == "run_her"
        assert calls[-1][1]["command"]["recovery"] == recovery
        assert calls[-1][1]["binding"] == binding
    else:
        assert provider._provider_context_observer is original_observer
        assert provider._provider_context_recovery == recovery
        assert calls[-1][0] == "local_turn"
# HASHI-Fingerprint: hfp_05fac5959c5c4865b9e323a63a903ea3
