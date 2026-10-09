import base64
import io
import json
from types import SimpleNamespace
import pytest
from aiohttp import web, ClientSession
from PIL import Image
from orchestrator.frontend_call.adapters import MediaAdapters
from orchestrator.frontend_call.contract import CallError, decode_jpeg
from orchestrator.frontend_call.routes import register_call_api
from orchestrator.enterprise.secret_refs import ConnectorSecretResolver
from test_call_contract import wav


async def serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


async def test_adapter_exercises_real_multipart_json_audio_and_no_redirect(monkeypatch):
    app = web.Application()
    seen = []

    async def stt(request):
        data = await request.post()
        assert data["model"] == "fixture"
        assert data["file"].file.read()[:4] == b"RIFF"
        seen.append("stt")
        assert request.headers["Authorization"] == "Bearer secret-fixture"
        return web.json_response({"text": "你好，HASHI implementation。"})

    async def tts(request):
        body = await request.json()
        assert body["input"] == "Hello" and body["voice"] == "warm"
        seen.append("tts")
        return web.Response(body=base64.b64decode(wav()), content_type="audio/wav")

    async def vision(request):
        body = await request.json()
        assert body["messages"][1]["content"][1]["image_url"]["url"].startswith(
            "data:image/jpeg;base64,"
        )
        return web.json_response(
            {"choices": [{"message": {"content": "A book is visible."}}]}
        )

    app.router.add_post("/v1/audio/transcriptions", stt)
    app.router.add_post("/v1/audio/speech", tts)
    app.router.add_post("/v1/chat/completions", vision)
    runner, url = await serve(app)
    try:
        monkeypatch.setenv("CALL_TEST_KEY", "secret-fixture")
        target = {
            "base_url": url + "/v1",
            "model": "fixture",
            "credential_env": "CALL_TEST_KEY",
            "audio_format": "wav",
        }
        adapters = MediaAdapters()
        assert (
            await adapters.transcribe(target, {"options": {}}, base64.b64decode(wav()))
        )["text"].startswith("你好")
        assert (
            await adapters.synthesize(
                target, {"voice_id": "warm", "options": {}}, "Hello"
            )
        )["media_type"] == "audio/wav"
        assert (
            await adapters.observe(
                target, {"options": {}}, b"fixture-image", "what is it?"
            )
            == "A book is visible."
        )
        assert seen == ["stt", "tts"]
    finally:
        await runner.cleanup()


async def test_provider_errors_do_not_disclose_body_or_key(monkeypatch):
    app = web.Application()

    async def reject(request):
        return web.Response(status=401, text="private account TOKEN=secret")

    app.router.add_post("/audio/transcriptions", reject)
    runner, url = await serve(app)
    try:
        with pytest.raises(CallError) as caught:
            await MediaAdapters().transcribe(
                {"base_url": url, "model": "m"},
                {"options": {}},
                base64.b64decode(wav()),
            )
        assert str(caught.value) == "call_provider_rejected"
    finally:
        await runner.cleanup()


async def test_openrouter_media_returns_without_secondary_provider_queries(monkeypatch):
    app = web.Application()
    lookups = []

    async def stt(request):
        assert request.headers["Authorization"] == "Bearer test-secret"
        form = await request.post()
        assert form["model"] == "openai/whisper-large-v3"
        assert form["file"].file.read()[:4] == b"RIFF"
        return web.json_response(
            {"text": "你好，测试成功。"},
            headers={"X-Generation-Id": "gen-stt-1"},
        )

    async def tts(request):
        body = await request.json()
        assert body["model"] == "google/gemini-3.8-flash-lite-tts"
        assert body["input"] == "你好，测试成功。"
        assert body["voice"] == "Sulafat"
        assert body["response_format"] == "pcm"
        assert body["provider"] == {
            "options": {
                "google-ai-studio": {"speech_metadata": {"style": "warm and clear"}}
            }
        }
        assert "style" not in body
        return web.Response(
            body=b"\0\x01" * 4800,
            content_type="audio/pcm",
            headers={"X-Generation-Id": "gen-tts-1"},
        )

    async def generation(request):
        generation_id = request.query["id"]
        lookups.append(generation_id)
        provider = {"gen-stt-1": "Groq", "gen-tts-1": "Google AI Studio"}[
            generation_id
        ]
        return web.json_response(
            {"data": {"id": generation_id, "provider_name": provider}}
        )

    app.router.add_post("/v1/audio/transcriptions", stt)
    app.router.add_post("/v1/audio/speech", tts)
    app.router.add_get("/v1/generation", generation)
    runner, url = await serve(app)
    try:
        monkeypatch.setattr(
            "orchestrator.frontend_call.adapters.OPENROUTER_API_BASE",
            url + "/v1",
        )
        adapters = MediaAdapters(
            secret_resolver=ConnectorSecretResolver(
                secrets={"openrouter-api_key": "test-secret"}
            )
        )
        base = {
            "base_url": url + "/v1",
            "credential_ref": "secrets://openrouter-api_key",
        }
        transcript = await adapters.transcribe(
            {**base, "model": "openai/whisper-large-v3"},
            {"options": {}},
            base64.b64decode(wav()),
        )
        assert transcript["text"] == "你好，测试成功。"
        assert transcript["provider_receipt"] is None
        speech = await adapters.synthesize(
            {
                **base,
                "model": "google/gemini-3.8-flash-lite-tts",
                "audio_format": "pcm",
            },
            {"voice_id": "Sulafat", "options": {"style": "warm and clear"}},
            transcript["text"],
        )
        assert speech["provider_receipt"] is None
        assert speech["media_type"] == "audio/wav"
        assert base64.b64decode(speech["content_b64"]).startswith(b"RIFF")
        assert lookups == []
    finally:
        await runner.cleanup()


def test_jpeg_dimensions_checked_without_ocr():
    out = io.BytesIO()
    Image.new("RGB", (640, 480)).save(out, format="JPEG")
    assert decode_jpeg(base64.b64encode(out.getvalue()).decode()) == out.getvalue()
    out = io.BytesIO()
    Image.new("RGB", (2050, 10)).save(out, format="JPEG")
    with pytest.raises(CallError, match="dimensions"):
        decode_jpeg(base64.b64encode(out.getvalue()).decode())
    with pytest.raises(CallError):
        decode_jpeg(base64.b64encode(b"\xff\xd8fake\xff\xd9").decode())


async def test_route_auth_json_and_disable_fail_closed(tmp_path):
    api = SimpleNamespace(
        app=web.Application(),
        config_path=tmp_path / "agents.json",
        admin_token="test-token",
        live_voice_manager=SimpleNamespace(),
        _is_governed_profile=lambda: False,
        _check_admin_auth=lambda request: (
            request.headers.get("X-Workbench-Token") == "test-token"
        ),
        _v1_owner_id=lambda request: "owner",
    )
    register_call_api(api)
    runner, url = await serve(api.app)
    try:
        async with ClientSession() as session:
            async with session.post(
                url + "/api/v1/call/operation", json={"operation": "context"}
            ) as response:
                assert response.status == 403
            (tmp_path / "call_profiles.json").write_text(json.dumps({"version": 1, "enabled": False}))
            async with session.post(
                url + "/api/v1/call/operation",
                json={},
                headers={"X-Workbench-Token": "test-token"},
            ) as response:
                assert (await response.json())["error_code"] == "call_disabled"
    finally:
        await runner.cleanup()
