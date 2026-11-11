import asyncio
from types import SimpleNamespace
import pytest
from orchestrator.runtime_app_host import RuntimeAppHost


@pytest.mark.asyncio
async def test_commit_returns_while_telegram_initialization_is_still_waiting(monkeypatch):
    host = RuntimeAppHost(None, {})
    entered = asyncio.Event()
    release = asyncio.Event()
    live = asyncio.create_task(release.wait())
    host.task = live
    app = SimpleNamespace(
        _shared_replacement_candidate=True, startup_status={"services_ready": True},
        function_workers=SimpleNamespace(), api_gateway=None,
        startup_manager=SimpleNamespace(reconcile_connector_status=lambda **kw: None))
    host.app = app
    persisted = []
    monkeypatch.setattr("orchestrator.runtime_app_host.runtime_handoff.persist", lambda a: persisted.append(a))

    async def blocked_connector():
        entered.set()
        await release.wait()
        return {}
    host._activate_connectors = blocked_connector
    try:
        result = await asyncio.wait_for(host.commit(), 0.5)
        assert result["committed"] is True
        await asyncio.wait_for(entered.wait(), 0.5)
        assert host.connector_task is not None and not host.connector_task.done()
        assert app._shared_committed is True
        assert persisted
    finally:
        release.set()
        if host.connector_task:
            await host.connector_task
        await live
# HASHI-Fingerprint: hfp_4975fc30e03c4b329821ae48adb7840a
