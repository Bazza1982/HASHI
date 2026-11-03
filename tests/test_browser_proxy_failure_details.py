"""Browser proxy errors reach the real isolated-tool consumer without action replay."""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import TimeoutError as FutureTimeout
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from tests.test_browser_gateway_proxy import _registry
from tools import browser_gateway_proxy as proxy_module
from tools.browser_gateway_proxy import BrowserGatewayProxy


@pytest.mark.asyncio
@pytest.mark.parametrize("error,reason", [(PermissionError("PRIVATE token=secret"), "access_denied"),
    (ValueError("PRIVATE URL"), "response_invalid"), (RuntimeError("PRIVATE body"), "internal_error")])
async def test_dispatched_browser_failure_has_safe_reference_in_actual_tool_output(tmp_path, monkeypatch, caplog, error, reason):
    owner = _registry(tmp_path, {})
    calls = []
    async def action(*args, **kwargs):
        calls.append(args)
        raise error
    monkeypatch.setattr(owner, "execute_with_audit_context", action)
    proxy = BrowserGatewayProxy(owner, asyncio.get_running_loop())
    try:
        descriptor = proxy.issue({"request_id": "request-safe", "global_config": SimpleNamespace(instance_id="HASHI5")})
        gateway = _registry(tmp_path, {"browser_gateway_proxy": descriptor,
            "global_config": SimpleNamespace(instance_id="HASHI5")})
        result = await gateway.execute("browser_get_text", {"url": "https://example.test"}, tool_call_id="call-safe")
        assert result.is_error is True and len(calls) == 1
        assert "operr_" in result.output and reason in result.output
        assert "PRIVATE" not in result.output and "secret" not in result.output
        records = [record.getMessage() for record in caplog.records if record.name == "HASHI.BrowserGateway"]
        assert any("operr_" in row and reason in row for row in records)
    finally:
        await asyncio.to_thread(proxy.close)


@pytest.mark.asyncio
async def test_malformed_failure_body_is_not_echoed_by_the_real_tool_consumer(tmp_path, monkeypatch):
    import io
    from tools import registry as registry_module
    gateway = _registry(tmp_path, {"browser_gateway_proxy": {"url": "http://127.0.0.1:18888/browser-tool", "token": "fixture"}})
    def invalid_response(request, **kwargs):
        raise HTTPError(request.full_url, 502, "PRIVATE", {}, io.BytesIO(b'{"ok":false,"message":"PRIVATE token=secret"}'))
    monkeypatch.setattr(registry_module.urllib_request, "urlopen", invalid_response)
    result = await gateway.execute("browser_get_text", {}, tool_call_id="call-invalid")
    assert result.is_error and "operr_" in result.output and "response_invalid" in result.output
    assert "PRIVATE" not in result.output and "secret" not in result.output


async def _post(proxy, descriptor, payload):
    def send():
        request = Request(descriptor["url"], data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + descriptor["token"], "Content-Type": "application/json"})
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        return response.status, response.read().decode()
    return await asyncio.to_thread(send)


@pytest.mark.asyncio
async def test_bad_arguments_keep_the_known_validation_and_do_not_dispatch(tmp_path, monkeypatch):
    owner = _registry(tmp_path, {})
    calls = []
    async def action(*args, **kwargs):
        calls.append(args)
    monkeypatch.setattr(owner, "execute_with_audit_context", action)
    proxy = BrowserGatewayProxy(owner, asyncio.get_running_loop())
    try:
        descriptor = proxy.issue({})
        status, text = await _post(proxy, descriptor, {"tool_name": "browser_get_text", "arguments": []})
        assert status == 400 and "arguments must be an object" in text
        assert calls == []
    finally:
        await asyncio.to_thread(proxy.close)


@pytest.mark.asyncio
async def test_timeout_keeps_specific_message_but_does_not_claim_remote_action_was_cancelled(tmp_path, monkeypatch):
    owner = _registry(tmp_path, {})
    proxy = BrowserGatewayProxy(owner, asyncio.get_running_loop())
    cancelled = []
    class Future:
        def result(self, **kwargs): raise FutureTimeout()
        def cancel(self): cancelled.append(True)
    def missing_ack(coroutine, loop):
        coroutine.close()
        return Future()
    monkeypatch.setattr(proxy_module.asyncio, "run_coroutine_threadsafe", missing_ack)
    try:
        descriptor = proxy.issue({})
        status, text = await _post(proxy, descriptor, {"tool_name": "browser_get_text", "arguments": {}})
        result = json.loads(text)
        assert status == 400 and "browser action timed out" in result["message"]
        assert result["accepted"] is None and result["retry_safe"] is False
        assert result["failure_phase"] == "dispatch" and len(cancelled) == 1
    finally:
        await asyncio.to_thread(proxy.close)
