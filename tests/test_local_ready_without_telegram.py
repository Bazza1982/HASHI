"""Local activation is independent of external connectors and their restoration."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import InvalidToken

from orchestrator.function_worker_host import FunctionWorkerHost, FunctionWorkerStateError
from orchestrator.startup_manager import StartupManager
from orchestrator.telegram_worker_lifecycle import start_connector_task
from orchestrator.her_v2.ledger import LedgerStore


def ready_host(tmp_path):
    host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    host.agent_name = "one"
    host.nonce = "current-nonce"
    host.manifest = SimpleNamespace(generation_id="current-generation")
    host.bridge_home = tmp_path
    host.phase = "READY"
    host.accepting = False
    host.adopted_at = None
    host.stop_event = asyncio.Event()
    host.legacy_reboot_bridge_task = None
    host.command_menu_task = None
    host.connector_task = None
    host._reconcile_interrupted_session_runs = AsyncMock(return_value=[])
    host._start_legacy_reboot_bridge = lambda: None
    host.emit_metadata = AsyncMock()
    host.metadata = lambda: {"worker_phase":host.phase,"worker_accepting":host.accepting,
                             "backend_ready":True,"telegram_connected":False}
    async def queue():
        await asyncio.Event().wait()
    app = SimpleNamespace(running=False, stop=AsyncMock(), shutdown=AsyncMock())
    host.runtime = SimpleNamespace(name="one",token="configured-token",app=app,
        telegram_connected=False,startup_success=False,_worker_version_metadata={},
        process_queue=queue,prepare_post_start_state=lambda: None)
    return host


@pytest.mark.asyncio
async def test_local_activation_never_waits_for_network_or_confirmation_restore(tmp_path):
    host = ready_host(tmp_path)
    network_started = asyncio.Event()
    async def offline():
        network_started.set()
        await asyncio.Event().wait()
    host._prepare_telegram_application = offline
    try:
        result = await asyncio.wait_for(host.activate(), 0.1)
        assert result["metadata"]["worker_phase"] == "ACTIVE" and host.accepting
        assert host.runtime.startup_success
        assert not network_started.is_set(), "uncommitted candidate must not restore Telegram cards"
        await host.handle_request("worker.connectors_start", {
            "worker_nonce":host.nonce,"generation_id":host.manifest.generation_id})
        await asyncio.wait_for(network_started.wait(), 0.1)
        assert host.phase == "ACTIVE" and host.accepting
    finally:
        tasks=[getattr(host,"connector_task",None),getattr(host.runtime,"process_task",None)]
        for task in tasks:
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in tasks if t),return_exceptions=True)


@pytest.mark.asyncio
async def test_connector_start_requires_current_active_worker_identity(tmp_path):
    host = ready_host(tmp_path)
    host._prepare_telegram_application = AsyncMock(return_value=True)
    with pytest.raises(FunctionWorkerStateError):
        await host.handle_request("worker.connectors_start", {
            "worker_nonce":"old-nonce","generation_id":host.manifest.generation_id})
    assert not host._prepare_telegram_application.called


@pytest.mark.asyncio
async def test_worker_invalid_token_stops_retry_until_token_revision_changes(tmp_path):
    host = ready_host(tmp_path)
    host.phase = "ACTIVE"
    host.accepting = True
    host.connector_owner = True
    calls = 0

    async def invalid_token():
        nonlocal calls
        calls += 1
        raise InvalidToken("credential rejected")

    host._prepare_telegram_application = invalid_token
    start_connector_task(host)
    task = host.connector_task
    await task

    assert calls == 1
    assert host.runtime._telegram_connector_attempts == 1
    assert host.runtime._telegram_connector_permanent_failure == "invalid_token"
    start_connector_task(host)
    assert host.connector_task is task
    assert calls == 1

    async def replacement_token_ready():
        nonlocal calls
        calls += 1
        host.runtime.telegram_connected = True
        return True

    host.runtime.token = "replacement-token"
    host._prepare_telegram_application = replacement_token_ready
    start_connector_task(host)
    replacement = host.connector_task
    assert replacement is not task
    await replacement
    assert calls == 2
    assert host.runtime._telegram_connector_attempts == 2
    assert host.runtime._telegram_connector_permanent_failure is None


@pytest.mark.asyncio
async def test_worker_transient_retry_is_single_owner_and_recovers(
    tmp_path, monkeypatch
):
    host = ready_host(tmp_path)
    host.phase = "ACTIVE"
    host.accepting = True
    host.connector_owner = True
    calls = 0

    async def transient_then_ready():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("offline")
        host.runtime.telegram_connected = True
        return True

    host._prepare_telegram_application = transient_then_ready
    monkeypatch.setattr(
        "orchestrator.telegram_worker_lifecycle.TELEGRAM_CONNECT_RETRY_SECONDS",
        0.001,
    )
    start_connector_task(host)
    first = host.connector_task
    start_connector_task(host)
    assert host.connector_task is first
    await asyncio.wait_for(first, timeout=0.2)
    assert calls == 2
    assert host.runtime._telegram_connector_attempts == 2


def test_global_local_ready_does_not_depend_on_telegram_or_retry():
    handle=SimpleNamespace(metadata={"worker_phase":"ACTIVE","worker_accepting":True},
        backend_ready=True,telegram_connected=False,client=SimpleNamespace(closed=False))
    workers=SimpleNamespace(_telegram_ingress={"one":object()},
        telegram_ingress_snapshot=lambda name: {"configured":True,"running":True,"connected":False})
    kernel=SimpleNamespace(function_workers=workers,_runtime_map=lambda:{"one":handle},
        startup_status={"services_ready":True,"phase":"ready","agent_states":{"one":"local"},
                        "failed_agents":0,"issues":[
                            {"code":"remote_unavailable","component":"remote","severity":"warning"},
                            {"code":"connector_activation","component":"connectors","severity":"warning"},
                        ]},
        remote_lifecycle_status={"enabled":True,"available":False})
    StartupManager(kernel,None).reconcile_connector_status(
        {"whatsapp":"offline"}, pending=True, publish=False
    )
    assert kernel.startup_status["ready"] is True
    assert kernel.startup_status["services_ready"] is True


@pytest.mark.parametrize(
    ("permanent_failure", "automatic_retry"),
    [("invalid_token", False), (None, True)],
)
def test_connector_health_reports_whether_telegram_retry_is_possible(
    permanent_failure, automatic_retry
):
    handle = SimpleNamespace(
        metadata={"worker_phase": "ACTIVE", "worker_accepting": True},
        backend_ready=True,
        telegram_connected=False,
        client=SimpleNamespace(closed=False),
    )
    ingress = SimpleNamespace(permanent_failure_reason=permanent_failure)
    workers = SimpleNamespace(
        _telegram_ingress={"one": ingress},
        telegram_ingress_snapshot=lambda name: {
            "configured": True,
            "running": False,
            "connected": False,
        },
    )
    kernel = SimpleNamespace(
        function_workers=workers,
        _runtime_map=lambda: {"one": handle},
        startup_status={
            "services_ready": True,
            "phase": "ready",
            "agent_states": {"one": "local"},
            "failed_agents": 0,
            "issues": [],
        },
    )

    StartupManager(kernel, None).reconcile_connector_status(publish=False)

    issue = next(
        item
        for item in kernel.startup_status["issues"]
        if item["code"] == "agent_telegram_unavailable"
    )
    assert issue["automatic_retry"] is automatic_retry


def test_no_herv3_recovery_candidates_means_no_historical_ledger_scan(tmp_path,monkeypatch):
    store=LedgerStore(tmp_path/"ledgers")
    def forbidden():
        raise AssertionError("empty canonical recovery set must not scan history")
    monkeypatch.setattr(store,"all_ledgers",forbidden)
    assert store.reconcile_interrupted(request_refs=set()) == ()
