from __future__ import annotations
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.phone_manager import PhoneManager
from orchestrator.frontend_live_voice.provider import select_provider
from orchestrator.frontend_live_voice.simulmt_provider import SimulMTProvider
from orchestrator.frontend_live_voice.manager import LiveVoiceManager
from orchestrator.frontend_live_voice.protocol import LiveVoiceError


def test_interpreter_projection_uses_direction_without_agent_context(tmp_path):
    manager = PhoneManager(tmp_path)
    manager.set_provider("hy-simulmt")
    manager.set_model("hy-simulmt-zh-en")
    result = manager.resolve_live_session(display_name="Private persona",
        pcm_payload={"secret": "PRIVATE_CONTEXT"}, recent_history=[{"text": "PRIVATE_HISTORY"}])
    assert result["public"]["mode"] == "interpretation"
    assert result["public"]["language"] == "en"
    assert result["public"]["persona_projected"] is False
    assert result["input"] == []
    assert "PRIVATE_" not in json.dumps(result)
    assert "Chinese speech into English" in result["instructions"]
    assert manager.get_state()["language"] == "en"
    assert {key for key, _ in manager.language_options()} == {"en", "zh-CN"}
    manager.set_language("zh-CN")
    assert manager.get_state()["model"] == "hy-simulmt-en-zh"
    assert manager.resolve_live_session(display_name="Interpreter")["public"]["language"] == "zh-CN"
    assert manager.style_options() == (("natural", "Natural"),)



def test_translation_does_not_claim_conversational_capabilities():
    adapter = SimulMTProvider()
    assert select_provider({"hy-simulmt": adapter}, "hy-simulmt") is adapter
    assert adapter.capabilities.proactive_opening is False
    assert adapter.capabilities.task_results is False
    assert adapter.capabilities.context_updates is False
    assert adapter.media_descriptor() == {
        "transport": "webrtc", "protocol": "openai-live-v1", "data_channel": "oai-events", "version": 1}
    fake = SimpleNamespace(_closing=False, _provider_for=lambda _: adapter)
    LiveVoiceManager._schedule_opening(fake, object())


@pytest.mark.asyncio
async def test_interpretation_streaming_pins_lan_dns_and_rejects_rebinding(monkeypatch):
    import socket
    from aiohttp import web
    from orchestrator import lan_transport

    calls, resolutions = [], []

    async def create(request):
        calls.append((request.host, request.path))
        assert (await request.json())["session"]["model"] == "hy-simulmt-en-zh"
        return web.json_response({"session": {"id": "session-1"},
                                  "transport": {"sdp": "v=0\r\n"}})

    async def websocket(request):
        calls.append((request.host, request.path))
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.close()
        return ws

    async def redirect(request):
        calls.append((request.host, request.path))
        raise web.HTTPFound("http://outside.invalid/ws")

    app = web.Application()
    app.router.add_post("/create", create)
    app.router.add_get("/ws/session-1", websocket)
    app.router.add_get("/redirect", redirect)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    class Delegate:
        async def resolve(self, host, selected_port=0, family=socket.AF_UNSPEC):
            resolutions.append(host)
            address = "8.8.8.8" if len(resolutions) == 3 else "127.0.0.1"
            return [{"hostname": host, "host": address, "port": selected_port,
                     "family": socket.AF_INET, "proto": 0, "flags": 0}]
        async def close(self):
            pass

    private = lan_transport.is_private_lan_address
    monkeypatch.setattr(lan_transport, "is_private_lan_address",
                        lambda host: host == "127.0.0.1" or private(host))
    monkeypatch.setattr(lan_transport.aiohttp.resolver, "DefaultResolver", Delegate)
    root = f"http://translate.test:{port}"
    resolved = SimpleNamespace(
        snapshot=SimpleNamespace(node=object()),
        interface_url=lambda protocol, **kw: root + (
            "/create" if protocol == "hashi.phone.webrtc" else "/ws/" + kw["session_id"]))
    class Directory:
        client = SimpleNamespace(request_headers=lambda node: {})
        async def refresh(self):
            return SimpleNamespace(errors={}, catalogue=SimpleNamespace(resolve=lambda *args: resolved))
    provider = SimulMTProvider(directory=Directory(), node_id="node", service_id="service")
    try:
        async with provider.http_session() as http:
            result = await provider.create(http, key="", sdp="v=0\r\n",
                                           model="hy-simulmt-en-zh", voice="default")
        assert result["provider_session_id"] == "session-1"
        async with provider.http_session() as http:
            ws = await provider.attach(http, key="", provider_session_id="session-1")
            await ws.close()
        with pytest.raises(lan_transport.LanDestinationError):
            async with provider.http_session() as http:
                await provider.attach(http, key="", provider_session_id="session-1")
        with pytest.raises(lan_transport.LanDestinationError):
            async with provider.http_session() as http:
                await http.ws_connect(f"ws://translate.test:{port}/redirect")
        assert calls == [(f"translate.test:{port}", "/create"),
                         (f"translate.test:{port}", "/ws/session-1"),
                         (f"translate.test:{port}", "/redirect")]
        assert resolutions == ["translate.test"] * 4
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_localhost_streaming_resolves_only_to_loopback():
    from orchestrator.lan_transport import PrivateLanResolver

    class Delegate:
        async def resolve(self, host, port=0, family=0):
            return [{"host": "192.168.1.9"}, {"host": "8.8.8.8"},
                    {"host": "127.0.0.1"}, {"host": "::1"}]
        async def close(self):
            pass

    resolver = PrivateLanResolver(Delegate(), allow_localhost=True)
    assert [row["host"] for row in await resolver.resolve("localhost")] == [
        "127.0.0.1", "::1"]
    await resolver.close()


def test_translation_sideband_accepts_subtitles_and_rejects_delegation():
    adapter = SimulMTProvider()
    event = adapter.normalize_event(json.dumps({"type": "session.output_transcript.delta",
        "event_id": "evt-1", "delta": "你好", "start_ms": 0, "end_ms": 1600}))
    assert event["type"] == "conversation.assistant.delta"
    assert event["delta"] == "你好"
    with pytest.raises(LiveVoiceError, match="live_interpretation_delegation_forbidden"):
        adapter.normalize_event(json.dumps({"type": "session.delegation.created",
            "delegation": {"id": "task-1", "target": "client"}}))
    with pytest.raises(LiveVoiceError):
        adapter.opening("say hello", "event-1")


@pytest.mark.parametrize("model,saved_language,target", [
    ("hy-simulmt-en-zh", "en", "zh-CN"),
    ("hy-simulmt-zh-en", "zh-CN", "en"),
    ("hy-simulmt-en-zh", "auto", "zh-CN"),
    ("hy-simulmt-zh-en", "ja", "en"),
])
def test_interpreter_model_owns_direction_in_saved_and_frozen_selection(tmp_path, model, saved_language, target):
    manager = PhoneManager(tmp_path)
    state = dict(manager.DEFAULT_STATE, provider="hy-simulmt", model=model,
                 voice="default", language=saved_language)
    original = json.dumps(state)
    manager.state_path.write_text(original, encoding="utf-8")
    assert manager.get_state()["language"] == target
    result = manager.resolve_live_session(display_name="Interpreter", frozen_selection=state)
    assert result["model"] == model
    assert result["selection"]["language"] == result["public"]["language"] == target
    assert manager.state_path.read_text(encoding="utf-8") == original
