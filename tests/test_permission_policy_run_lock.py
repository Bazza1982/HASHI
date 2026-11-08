"""PAO's live Run barrier must not deadlock the installed policy reader."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.pao_run_fence import PaoRunFence
from tests import test_permission_baseline_execution as baseline_fixture
from tests.test_permission_execution_gate import CONTRACT_WATCHDOG_SECONDS

installed_policy = baseline_fixture.installed


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["L1", "L2"])
async def test_installed_policy_reads_committed_workzones_during_native_run_barrier(installed_policy, level):
    env = await installed_policy(("read",), ("create", "write"), level=level, workzone=True)
    binding = env.executor.binding
    guard = PaoRunFence(
        env.store, context=env.context, request_id=binding.request_id,
        fencing_token=binding.fencing_token, worker_id=binding.worker_id,
    )
    entered, finished = Event(), Event()
    pool = ThreadPoolExecutor(max_workers=1)
    original = env.policies._workzones

    def observe(*args, **kwargs):
        entered.set()
        return original(*args, **kwargs)

    env.policies._workzones = observe

    def read_baseline():
        try:
            return env.authority.evaluate(env.context, env.policy.baseline_scopes, operation_id="concurrent-preflight")
        finally:
            finished.set()

    future = None
    try:
        with guard(env.context):
            future = pool.submit(read_baseline)
            assert entered.wait(CONTRACT_WATCHDOG_SECONDS), "policy reader did not reach PAO"
            read_during_barrier = finished.wait(CONTRACT_WATCHDOG_SECONDS)
        # Always release the real barrier before asserting, including on RED.
        baseline = future.result(timeout=CONTRACT_WATCHDOG_SECONDS)
        assert read_during_barrier, "policy reader waits for the Run mutex while holding the authority transaction"
        assert baseline is True

        # A separate reader must still observe committed changes, never a cache.
        replacement = env.root.parent / "changed-workzone"
        replacement.mkdir()
        env.store.set_agent_workzone_slot(
            owner_id="owner", agent_id="agent", slot_id="main",
            path=str(replacement), expected_revision=1,
        )
        with pytest.raises(PermissionDenied, match="policy changed"):
            env.policies.baseline(env.context)
    finally:
        if future is not None:
            future.result(timeout=CONTRACT_WATCHDOG_SECONDS)
        pool.shutdown(wait=True)
        await env.executor.close()
# HASHI-Fingerprint: hfp_78818d4639a846bc98a67490bf00dafb
