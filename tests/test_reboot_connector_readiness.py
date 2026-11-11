"""A committed local replacement must not wait for optional channel recovery."""
import json
from types import SimpleNamespace
import pytest
from tests.test_reboot_manager import _Kernel, _Generation, _Client, _metadata
from orchestrator.function_worker_supervisor import AgentRuntimeHandle
from orchestrator.reboot_manager import RebootManager


async def successor(tmp_path, monkeypatch, startup):
    old = _Kernel()
    old.paths = SimpleNamespace(bridge_home=tmp_path)
    manager = RebootManager(old, None)
    assert await manager.hot_restart({"mode": "max"})
    pending = manager.receipts.records()[-1]
    generation = _Generation("a")
    kernel = _Kernel()
    kernel.paths = SimpleNamespace(bridge_home=tmp_path)
    kernel._shared_replacement_continuity = True
    kernel.shared_generation_id = generation.manifest.generation_id
    kernel.startup_status = startup
    kernel._connector_activation_pending = True
    kernel.runtimes = []
    for index, name in enumerate(("zelda", "sunny"), start=1):
        client = _Client(name, 200 + index, generation, kernel.events)
        kernel.runtimes.append(AgentRuntimeHandle(kernel, client, _metadata(name, client.pid, generation, telegram=False)))
    state = tmp_path / "state/instance"
    (state / "kernel-requests" / f"{pending['id']}.json").unlink()
    (state / f"replacement-{pending['id']}.json").write_text(
        json.dumps({"ok": True, "generation_id": generation.manifest.generation_id}))
    monkeypatch.setattr("orchestrator.reboot_manager.os.getpid",
                        lambda: int(pending["shared_replacement"]["old_shared_pid"]) + 1)
    result = RebootManager(kernel, None)
    result.receipts.recover()
    return result, pending


@pytest.mark.asyncio
async def test_telegram_warning_is_durable_success_even_while_channel_activation_is_pending(tmp_path, monkeypatch):
    manager, pending = await successor(tmp_path, monkeypatch, {
        "services_ready": True, "ready": False, "degraded": True, "failed_agents": 0,
        "remote": {"enabled": True, "available": True},
        "issues": [{"code": "agent_telegram_unavailable", "severity": "warning", "component": "telegram"}],
    })
    await manager.reconcile_shared_replacements(now=pending["shared_replacement"]["requested_at"] + 1)
    record = manager.receipts.get(pending["id"])
    assert record["status"] == "succeeded"
    assert record["outcome"] == "succeeded_with_warning"
    assert "connectors_unavailable" in record["warnings"]
    assert all(record["online"].values())
    projection = manager._operation_projection(record)
    assert projection["terminal"] is True
    assert projection["warnings"] == record["warnings"]
    manager.receipts.recover()
    assert manager.receipts.get(pending["id"])["status"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"services_ready": False},
    {"failed_agents": 1},
    {"issues": [{"code": "local_service_failure", "severity": "error"}]},
])
async def test_local_service_failures_are_terminal_and_never_waived_as_channel_warnings(tmp_path, monkeypatch, changes):
    startup = {"services_ready": True, "ready": False, "degraded": True,
               "failed_agents": 0, "issues": [], **changes}
    manager, pending = await successor(tmp_path, monkeypatch, startup)
    await manager.reconcile_shared_replacements(now=pending["shared_replacement"]["requested_at"] + 1)
    record = manager.receipts.get(pending["id"])
    assert manager._operation_projection(record)["terminal"] is True
    assert record["status"] != "succeeded"
    assert record["committed"] is True
# HASHI-Fingerprint: hfp_07b12155236348fd900284d00d1e1276
