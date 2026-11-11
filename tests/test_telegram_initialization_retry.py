"""Real PTB request clients must be closed after failed or cancelled initialization."""
import asyncio
import httpx
import pytest
from telegram.ext import Application
from telegram.request import HTTPXRequest
from tests.test_local_ready_without_telegram import ready_host


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "timeout"])
async def test_initialization_retry_resets_real_ptb_clients(tmp_path, monkeypatch, failure):
    calls = 0
    async def api(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "network":
                raise httpx.ConnectError("offline", request=request)
            await asyncio.Event().wait()
        return httpx.Response(200, json={"ok":True,"result":{
            "id":123,"is_bot":True,"first_name":"Fixture","username":"fixture_bot"}})
    req = HTTPXRequest(httpx_kwargs={"transport":httpx.MockTransport(api)})
    original_client = req._client
    app = Application.builder().token("123:fixture").request(req).updater(None).build()
    host = ready_host(tmp_path)
    host.runtime.token = "123:fixture"
    host.runtime.app = app
    monkeypatch.setattr("orchestrator.telegram_freshness_gate.restore_telegram_confirmation_state",
        lambda _: asyncio.sleep(0))
    async def menu(_): pass
    host._sync_telegram_command_menus = menu
    try:
        with pytest.raises((TimeoutError, Exception)) as caught:
            await asyncio.wait_for(host._prepare_telegram_application(), .05)
        assert type(caught.value).__name__ in {"NetworkError","TimeoutError"}
        if failure == "network":
            assert req._client.is_closed  # Application.shutdown alone skips partial initialization.
        assert await host._prepare_telegram_application() is True
        assert original_client.is_closed and req._client is not original_client
        assert app.running and host.runtime.telegram_connected
        assert calls == 2
    finally:
        await host._cancel_connector_tasks()
        if app.running:
            await app.stop()
        await app.shutdown()
        await app.bot.shutdown()
# HASHI-Fingerprint: hfp_4fe995f6f2a747379076547b07b8c53d
